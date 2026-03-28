"""
routes.py – Flask routes for the web interface and upload API.

Contains:
  - GET  /              -> public login page (unauthenticated)
  - GET  /dashboard     -> avatar upload / crop page (authenticated)
  - GET  /user-avatars/ -> serve stored avatar files
  - POST /api/upload    -> accept cropped image, process, update backends
"""

import io
import json
import logging
from datetime import datetime, timezone

from PIL import Image

from flask import Blueprint, redirect, url_for, session, request, jsonify, send_from_directory, render_template, Response, stream_with_context

from src.config import img_cfg, ak_cfg, app_cfg, dry_run
from src.i18n import t
from src.auth import login_required
from src.imaging import (
    AVATAR_ROOT, METADATA_ROOT, ALLOWED_EXTENSIONS, ALLOWED_FORMATS, MIN_DIMENSION, MAX_DIMENSION,
    MAX_SIZE, _FORMAT_MAP, normalize_image, check_magic_bytes, generate_filename, process_image,
    cleanup_avatar_files, prepare_ldap_image,
)
from src.authentik_api import update_avatar_url
from src.ldap_client import update_photos as update_ldap_photos, is_enabled as ldap_is_enabled, get_photos_config as ldap_photos_config

# Cache immutable config values at module level (config never changes after startup)
_ldap_enabled       = ldap_is_enabled()
_ldap_photos        = ldap_photos_config()
_ak_avatar_size     = ak_cfg.get('avatar_size', 1024)
log = logging.getLogger('routes')

routes_bp = Blueprint('routes', __name__)


# ---------------------------------------------------------------------------
# robots.txt – block all search engine crawling
# ---------------------------------------------------------------------------
@routes_bp.route('/robots.txt')
def robots_txt():
    """Serve robots.txt from the in-memory static cache."""
    from app import _static_cache
    entry = _static_cache.get('robots.txt')
    if entry is None:
        return Response('', status=404)
    data, mime, _ = entry
    return Response(data, mimetype=mime)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@routes_bp.route('/healthz')
def healthz():
    """Lightweight health probe for load balancers and orchestrators."""
    return Response('OK', mimetype='text/plain')


# ---------------------------------------------------------------------------
# Public login page
# ---------------------------------------------------------------------------
@routes_bp.route('/')
def login_page():
    """Show a static landing page with a login button, or redirect to dashboard if already authenticated."""
    if 'user' in session:
        log.debug('User already authenticated – redirecting to dashboard.')
        return redirect(url_for('routes.dashboard'))
    error_key = request.args.get('error', '')
    if not error_key and 'autologin' in request.args:
        return redirect(url_for('auth.login'))
    if error_key:
        log.debug('Login page rendered with error=%r.', error_key)
    return render_template('login.html', error_key=error_key)


# ---------------------------------------------------------------------------
# Dashboard (authenticated)
# ---------------------------------------------------------------------------
@routes_bp.route('/dashboard')
@login_required
def dashboard():
    """Serve the authenticated avatar upload / crop page."""
    user = session['user']
    log.debug('Serving dashboard for user %r.', user['username'])
    return render_template('dashboard.html', user=user, ldap_enabled=_ldap_enabled, max_size=MAX_SIZE)


# ---------------------------------------------------------------------------
# Serve stored avatar files
# ---------------------------------------------------------------------------
@routes_bp.route('/user-avatars/<path:filepath>')
def serve_avatar(filepath):
    """Serve avatar files from the storage directory. `send_from_directory` prevents directory-traversal attacks."""
    log.debug('Serving avatar file: %s', filepath)
    return send_from_directory(AVATAR_ROOT, filepath)


# ---------------------------------------------------------------------------
# Upload & process API  (Server-Sent Events for real-time progress)
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as a single Server-Sent Event frame."""
    return f"data: {json.dumps(data)}\n\n"


@routes_bp.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """
    Accept a cropped image blob, validate it synchronously, then stream
    processing progress back to the client as Server-Sent Events.

    Validation failures return a normal JSON 400 response.
    Once validation passes the response switches to ``text/event-stream``
    and each processing step is pushed as it completes.
    """
    user = session['user']
    log.info('Upload request from user %r.', user['username'])

    # Validate the upload (synchronous – returns JSON 400 on failure)
    if 'file' not in request.files:
        log.warning('Upload rejected – no file part in request.')
        return jsonify({'error': 'No file part in the request.'}), 400

    # Check filename
    file = request.files['file']
    if not file.filename:
        log.warning('Upload rejected – empty filename.')
        return jsonify({'error': 'Empty filename.'}), 400

    # Check extension allow-list (based on filename only, as a first quick check before reading file bytes and checking magic signatures)
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        log.warning('Upload rejected – disallowed extension .%s.', ext)
        return jsonify({'error': f'File type .{ext} not allowed. Accepted: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400

    # Read file bytes into memory (Pillow will re-parse from bytes, so we can safely close the file after this)
    log.debug('Reading uploaded file bytes.')
    raw_bytes = file.read()
    if not raw_bytes:
        return jsonify({'error': 'Uploaded file is empty.'}), 400

    # Check magic bytes to prevent fake extensions (e.g. .jpg that is actually a .exe).
    # This is not a security boundary but helps catch invalid uploads early and provides better error messages.
    magic_err = check_magic_bytes(raw_bytes)
    if magic_err:
        log.warning('Upload rejected – magic byte check failed: %s', magic_err)
        return jsonify({'error': magic_err}), 400
    log.debug('Magic-byte signature check passed.')

    # Validate that Pillow can decode the image.
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        # .load() forces full pixel decoding.  This catches truncated,
        # corrupt, or intentionally malformed files that Image.open()
        # (which only reads the header) would not detect.
        image.load()
    except Exception as exc:
        log.warning('Upload rejected – Pillow could not decode image: %s', exc)
        return jsonify({'error': f'Cannot decode image: {exc}'}), 400

    # A file can have a .png extension but actually be a TIFF, BMP, or something else
    # Pillow happens to support — only allow formats we explicitly intend to handle.
    if image.format not in ALLOWED_FORMATS:
        log.warning('Upload rejected – decoded format %r is not in the allow-list %s.', image.format, ALLOWED_FORMATS)
        return jsonify({'error': f'Image format {image.format!r} is not allowed.'}), 400
    log.debug('Decoded format %s is in the allow-list.', image.format)

    # Reject images too small to resize up or too large for reasonable CPU/memory use.
    w, h = image.size
    if w < MIN_DIMENSION or h < MIN_DIMENSION:
        log.warning('Upload rejected – image too small: %dx%d (min %d).', w, h, MIN_DIMENSION)
        return jsonify({'error': f'Image is too small ({w}x{h}). Minimum dimension is {MIN_DIMENSION}px.'}), 400
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        log.warning('Upload rejected – image too large: %dx%d (max %d).', w, h, MAX_DIMENSION)
        return jsonify({'error': f'Image is too large ({w}x{h}). Maximum dimension is {MAX_DIMENSION}px.'}), 400
    log.debug('Dimensions %dx%d are within acceptable range.', w, h)

    log.debug(
        'Upload metadata: filename=%r, content_type=%r, size=%d bytes, dimensions=%dx%d, mode=%s, format=%s.',
        file.filename, file.content_type, len(raw_bytes), w, h, image.mode, image.format,
    )
    if image.info:
        log.debug('Image info: %s', {k: v for k, v in image.info.items() if not isinstance(v, bytes)})

    image = normalize_image(image)
    log.info('Image validated – mode=%s, size=%dx%d. Starting SSE stream.', image.mode, image.width, image.height)

    # -- Stream processing steps as Server-Sent Events ----------------------

    def _build_ldap_updates(urls: dict, filename_base: str) -> list[dict]:
        """
        Build the list of LDAP attribute updates from the configured photo
        entries.  For ``binary`` types the image is encoded on-the-fly (or
        reused from disk); for ``url`` types the pre-generated URL is looked up.
        """
        updates = []
        for photo_cfg in _ldap_photos:
            attr = photo_cfg['attribute']
            ptype = photo_cfg['type']
            size = photo_cfg['image_size']
            img_type = photo_cfg['image_type']

            if ptype == 'binary':
                img_bytes = prepare_ldap_image(
                    image, filename_base, size, img_type,
                    photo_cfg.get('max_file_size', 0),
                )
                updates.append({'attribute': attr, 'value': img_bytes})
                log.info('Prepared LDAP %s: %dx%d %s, %d bytes.',
                         attr, size, size, img_type.upper(), len(img_bytes))

            elif ptype == 'url':
                size_key = f'{size}x{size}'
                ext = _FORMAT_MAP[img_type][1]
                url = urls.get(size_key, {}).get(ext)
                if not url:
                    raise ValueError(
                        f'No pre-generated URL for LDAP {attr}: '
                        f'size={size_key}, ext={ext}. Check images.sizes/formats config.'
                    )
                updates.append({'attribute': attr, 'value': url})
                log.info('Prepared LDAP %s: URL → %s.', attr, url)

            else:
                log.warning('Unknown LDAP photo type %r for attribute %s – skipping.', ptype, attr)

        return updates

    def generate():
        filename_base = None
        try:
            yield _sse({'step': t('step_validated'), 'status': 'success', 'detail': t('step_validated_detail')})

            # -- Step 1: Generate filename -------------------------------------
            filename_base = generate_filename()
            yield _sse({'step': t('step_filename'), 'status': 'success'})

            # -- Step 2: Resize & save all configured sizes/formats ------------
            urls, total_bytes = process_image(image, filename_base)
            if not urls:
                raise RuntimeError('Image processing returned no URLs – check images.sizes/formats config.')
            size_label = f'{total_bytes / 1_048_576:.1f} MB' if total_bytes >= 1_048_576 else f'{total_bytes / 1024:.0f} KB'
            yield _sse({'step': t('step_processed'), 'status': 'success', 'detail': t('step_processed_detail', sizes=len(img_cfg['sizes']), formats=len(img_cfg['formats']), total=size_label)})

            # -- Step 3: Resolve the canonical avatar URL for Authentik --------
            ak_size_key = f'{_ak_avatar_size}x{_ak_avatar_size}'
            canonical_url = urls.get(ak_size_key, {}).get('jpg')
            if not canonical_url:
                raise RuntimeError(
                    f'Canonical avatar URL not found for size={ak_size_key}, format=jpg. '
                    f'Ensure {_ak_avatar_size} is in images.sizes and "jpg" is in images.formats.'
                )
            log.debug('Canonical Authentik avatar URL: %s', canonical_url)

            # -- Step 4: Push avatar URL to Authentik --------------------------
            # Uses the PK stored in the session at login time.
            # The returned attributes dict is inspected below for ldap_uniq.
            has_failure = False
            ak_attrs = {}
            try:
                ak_attrs = update_avatar_url(user['pk'], canonical_url)
                if not isinstance(ak_attrs, dict):
                    raise TypeError(f'Authentik API returned {type(ak_attrs).__name__} instead of dict.')
                yield _sse({'step': t('step_profile_synced'), 'status': 'dry-run' if dry_run else 'success'})
            except Exception:
                log.exception('Failed to update Authentik avatar for pk=%s.', user['pk'])
                yield _sse({'step': t('step_profile_synced'), 'status': 'failed'})
                has_failure = True

            # -- Step 5: Update LDAP photo attributes (if applicable) ----------
            # Only attempted when LDAP is enabled, photos are configured, and
            # the user has a ldap_uniq attribute (proving they were synced from LDAP).
            if _ldap_enabled and _ldap_photos:
                ldap_uniq = ak_attrs.get('ldap_uniq')
                if ldap_uniq:
                    log.debug('User has ldap_uniq=%r – preparing %d LDAP photo update(s).', ldap_uniq, len(_ldap_photos))
                    try:
                        ldap_updates = _build_ldap_updates(urls, filename_base)
                        update_ldap_photos(ldap_uniq, ldap_updates)
                        detail = ', '.join(u['attribute'] for u in ldap_updates)
                        yield _sse({'step': t('step_ldap_updated'), 'status': 'dry-run' if dry_run else 'success', 'detail': detail})
                    except Exception:
                        log.exception('Failed to update LDAP photo attributes for ldap_uniq=%s.', ldap_uniq)
                        yield _sse({'step': t('step_ldap_updated'), 'status': 'failed'})
                        has_failure = True
                else:
                    log.info('User pk=%s has no ldap_uniq attribute – skipping LDAP photo updates.', user['pk'])
                    yield _sse({'step': t('step_ldap_updated'), 'status': 'skipped'})

            # -- Rollback on backend failure -----------------------------------
            if has_failure:
                log.warning('Backend update failed – cleaning up saved avatar files for %s.', filename_base)
                cleanup_avatar_files(filename_base)
                yield _sse({'done': True, 'error': 'Could not update your avatar. Please try again later.'})
                return

            # -- Step 6: Persist metadata JSON ---------------------------------
            # Uses the Authentik PK as the owner identifier (not the username)
            # because PKs are immutable and don't leak PII.
            metadata = {
                'filename': filename_base,
                'user_pk': user['pk'],
                'uploaded_at': datetime.now(timezone.utc).isoformat(),
                'sizes': img_cfg['sizes'],
                'formats': img_cfg['formats'],
                'authentik_avatar_url': canonical_url,
                'total_bytes': total_bytes,
            }
            meta_path = METADATA_ROOT / f'{filename_base}.meta.json'
            meta_path.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
            log.debug('Metadata saved to %s.', meta_path)

            log.info('Upload pipeline complete for user %r (pk=%s).', user['username'], user['pk'])
            yield _sse({'done': True, 'avatar_url': canonical_url})

        except Exception as exc:
            log.exception('Upload processing failed for user %r.', user['username'])
            # Clean up any files that were written before the failure
            if filename_base:
                cleanup_avatar_files(filename_base)
            yield _sse({'step': t('step_processing_failed'), 'status': 'failed', 'detail': str(exc)})
            yield _sse({'done': True, 'error': str(exc)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
