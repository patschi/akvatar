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

from PIL import Image, ImageOps
from pathlib import Path

from flask import Blueprint, redirect, url_for, session, request, jsonify, send_from_directory, render_template, Response, stream_with_context

from src.config import ldap_cfg, img_cfg, ak_cfg, dry_run
from src.i18n import t
from src.auth import login_required
from src.imaging import (
    AVATAR_ROOT, ALLOWED_EXTENSIONS, ALLOWED_FORMATS, MIN_DIMENSION, MAX_DIMENSION,
    MAX_SIZE, check_magic_bytes, generate_filename, process_image, cleanup_avatar_files,
)
from src.authentik_api import update_avatar_url
from src.ldap_client import update_thumbnail as update_ad_thumbnail, is_enabled as ldap_is_enabled

# Cache LDAP enabled state at module level (config is immutable after startup)
_ldap_enabled = ldap_is_enabled()

log = logging.getLogger('routes')

routes_bp = Blueprint('routes', __name__)

# Load robots.txt into memory once at import time
_ROBOTS_TXT = Path(__file__).resolve().parent.parent / 'static' / 'robots.txt'
_ROBOTS_CONTENT = _ROBOTS_TXT.read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# robots.txt – block all search engine crawling
# ---------------------------------------------------------------------------
@routes_bp.route('/robots.txt')
def robots_txt():
    """Serve robots.txt from memory."""
    return Response(_ROBOTS_CONTENT, mimetype='text/plain')


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
    log.debug('Serving login page.')
    return render_template('login.html')


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

    # Check that the decoded image format is in the allow-list.
    # A file can have a .png extension but actually be a TIFF, BMP, or
    # something else Pillow happens to support.  We only allow formats we
    # explicitly intend to handle.
    if image.format not in ALLOWED_FORMATS:
        log.warning('Upload rejected – decoded format %r is not in the allow-list %s.', image.format, ALLOWED_FORMATS)
        return jsonify({'error': f'Image format {image.format!r} is not allowed.'}), 400
    log.debug('Decoded format %s is in the allow-list.', image.format)

    # Dimension checks
    # Reject images that are unreasonably small (pointless to resize up) or
    # large (excessive CPU / memory during resize).
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

    # EXIF orientation
    # Photos from phones often carry an EXIF orientation tag rather than
    # having their pixels pre-rotated.  exif_transpose() reads that tag,
    # rotates the pixel data to match, and drops the tag so downstream
    # code sees the correct orientation without needing to understand EXIF.
    image = ImageOps.exif_transpose(image) or image
    log.debug('EXIF orientation applied.  Effective dimensions: %dx%d.', image.width, image.height)

    # Strip all metadata – rebuild from raw pixel data only
    # Re-create the image from raw pixel data only.  This discards EXIF,
    # ICC profiles, XMP, IPTC, comments, and any other ancillary chunks.
    # Why:
    #   - EXIF can leak PII (GPS, device model, timestamps).
    #   - Ancillary PNG/JPEG chunks can carry hidden payloads.
    #   - ICC profiles are unnecessary for avatar thumbnails.
    #   - Starting from a clean image guarantees nothing unexpected passes
    #     through to the saved output files.
    clean = Image.frombytes(image.mode, image.size, image.tobytes())
    image = clean
    log.debug('Metadata stripped – working with clean pixel-only image.')

    # Normalise colour mode
    if image.mode not in ('RGB', 'RGBA'):
        log.debug('Converting image mode %s -> RGBA.', image.mode)
        image = image.convert('RGBA')

    log.info('Image validated – mode=%s, size=%dx%d. Starting SSE stream.', image.mode, image.width, image.height)

    # -- Stream processing steps as Server-Sent Events ----------------------
    def generate():
        try:
            yield _sse({'step': t('step_validated'), 'status': 'success', 'detail': t('step_validated_detail')})

            # Generate filename
            filename_base = generate_filename()
            yield _sse({'step': t('step_filename'), 'status': 'success'})

            # Resize & save
            urls, total_bytes = process_image(image, filename_base)
            if total_bytes >= 1_048_576:
                size_label = f'{total_bytes / 1_048_576:.1f} MB'
            else:
                size_label = f'{total_bytes / 1024:.0f} KB'
            yield _sse({'step': t('step_processed'), 'status': 'success', 'detail': t('step_processed_detail', sizes=len(img_cfg['sizes']), formats=len(img_cfg['formats']), total=size_label)})

            ak_size = ak_cfg.get('avatar_size', 1024)
            log.debug('Using %dx%d JPG for Authentik avatar URL (from authentik_api.avatar_size).', ak_size, ak_size)
            canonical_url = urls[f'{ak_size}x{ak_size}']['jpg']
            ad_thumb_size = ldap_cfg.get('thumbnail_size', 128)
            has_failure = False

            # Update Authentik
            try:
                update_avatar_url(user['username'], canonical_url)
                status = 'dry-run' if dry_run else 'success'
                yield _sse({'step': t('step_profile_synced'), 'status': status})
            except Exception:
                log.exception('Failed to update Authentik avatar.')
                yield _sse({'step': t('step_profile_synced'), 'status': 'failed'})
                has_failure = True

            # Update AD (if enabled)
            if _ldap_enabled:
                try:
                    thumb_path = AVATAR_ROOT / f'{ad_thumb_size}x{ad_thumb_size}' / f'{filename_base}.jpg'
                    log.debug('Reading AD thumbnail from %s.', thumb_path)
                    jpeg_bytes = thumb_path.read_bytes()
                    update_ad_thumbnail(user['username'], jpeg_bytes)
                    status = 'dry-run' if dry_run else 'success'
                    yield _sse({'step': t('step_ad_updated'), 'status': status})
                except Exception:
                    log.exception('Failed to update AD thumbnailPhoto.')
                    yield _sse({'step': t('step_ad_updated'), 'status': 'failed'})
                    has_failure = True

            # Rollback on backend failure
            if has_failure:
                log.warning('Backend update failed – cleaning up saved avatar files.')
                cleanup_avatar_files(filename_base)
                yield _sse({'done': True, 'error': 'Could not update your avatar. Please try again later.'})
                return

            # Save metadata JSON
            metadata = {
                'filename': filename_base,
                'username': user['username'],
                'uploaded_at': datetime.now(timezone.utc).isoformat(),
                'sizes': img_cfg['sizes'],
                'formats': img_cfg['formats'],
                'authentik_avatar_url': canonical_url,
                'total_bytes': total_bytes,
            }
            meta_path = AVATAR_ROOT / f'{filename_base}.meta.json'
            meta_path.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
            log.debug('Metadata saved to %s.', meta_path)

            log.info('Upload pipeline complete for user %r.', user['username'])
            yield _sse({'done': True, 'avatar_url': canonical_url})

        except Exception as exc:
            log.exception('Upload processing failed.')
            yield _sse({'step': t('step_processing_failed'), 'status': 'failed', 'detail': str(exc)})
            yield _sse({'done': True, 'error': str(exc)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
