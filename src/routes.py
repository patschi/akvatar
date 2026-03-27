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
from flask import Blueprint, redirect, url_for, session, request, jsonify, send_from_directory, render_template

from src.config import ldap_cfg, img_cfg, ak_cfg, dry_run
from src.i18n import t
from src.auth import login_required
from src.imaging import (
    AVATAR_ROOT, ALLOWED_EXTENSIONS, ALLOWED_FORMATS, MIN_DIMENSION, MAX_DIMENSION,
    check_magic_bytes, generate_filename, process_image, cleanup_avatar_files,
)
from src.authentik_api import update_avatar_url
from src.ldap_client import update_thumbnail as update_ad_thumbnail, is_enabled as ldap_is_enabled

log = logging.getLogger('routes')

routes_bp = Blueprint('routes', __name__)


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
    max_size = max(img_cfg['sizes'])
    return render_template('dashboard.html', user=user, ldap_enabled=ldap_is_enabled(), max_size=max_size)


# ---------------------------------------------------------------------------
# Serve stored avatar files
# ---------------------------------------------------------------------------
@routes_bp.route('/user-avatars/<path:filepath>')
def serve_avatar(filepath):
    """Serve avatar files from the storage directory. `send_from_directory` prevents directory-traversal attacks."""
    log.debug('Serving avatar file: %s', filepath)
    return send_from_directory(AVATAR_ROOT, filepath)


# ---------------------------------------------------------------------------
# Upload & process API
# ---------------------------------------------------------------------------
@routes_bp.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """Accept a cropped image blob, resize/save it, update Authentik and (optionally) AD, return progress as JSON."""
    user = session['user']
    log.info('Upload request from user %r.', user['username'])

    # -- Validate the upload -----------------------------------------------
    if 'file' not in request.files:
        log.warning('Upload rejected – no file part in request.')
        return jsonify({'error': 'No file part in the request.'}), 400

    file = request.files['file']
    if not file.filename:
        log.warning('Upload rejected – empty filename.')
        return jsonify({'error': 'Empty filename.'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        log.warning('Upload rejected – disallowed extension .%s.', ext)
        return jsonify({'error': f'File type .{ext} not allowed. Accepted: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400

    steps: list[dict] = []

    try:
        # -- Step 1: Read raw bytes --------------------------------------------
        log.debug('Reading uploaded file bytes.')
        raw_bytes = file.read()
        if not raw_bytes:
            return jsonify({'error': 'Uploaded file is empty.'}), 400

        # -- Step 2: Magic-byte check ------------------------------------------
        # Verify file signature BEFORE passing bytes to Pillow.  This stops
        # crafted non-image files (HTML, SVG, ZIP polyglots) from reaching the
        # image decoder at all.
        magic_err = check_magic_bytes(raw_bytes)
        if magic_err:
            log.warning('Upload rejected – magic byte check failed: %s', magic_err)
            return jsonify({'error': magic_err}), 400
        log.debug('Magic-byte signature check passed.')

        # -- Step 3: Decode with Pillow ----------------------------------------
        try:
            image = Image.open(io.BytesIO(raw_bytes))
            # .load() forces full pixel decoding.  This catches truncated,
            # corrupt, or intentionally malformed files that Image.open()
            # (which only reads the header) would not detect.
            image.load()
        except Exception as exc:
            log.warning('Upload rejected – Pillow could not decode image: %s', exc)
            return jsonify({'error': f'Cannot decode image: {exc}'}), 400

        # -- Step 4: Verify decoded format is in the allow-list ----------------
        # A file can have a .png extension but actually be a TIFF, BMP, or
        # something else Pillow happens to support.  We only allow formats we
        # explicitly intend to handle.
        if image.format not in ALLOWED_FORMATS:
            log.warning('Upload rejected – decoded format %r is not in the allow-list %s.', image.format, ALLOWED_FORMATS)
            return jsonify({'error': f'Image format {image.format!r} is not allowed.'}), 400
        log.debug('Decoded format %s is in the allow-list.', image.format)

        # -- Step 5: Dimension checks ------------------------------------------
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

        # Log file and image metadata at debug level
        log.debug(
            'Upload metadata: filename=%r, content_type=%r, size=%d bytes, dimensions=%dx%d, mode=%s, format=%s.',
            file.filename, file.content_type, len(raw_bytes), w, h, image.mode, image.format,
        )
        if image.info:
            log.debug('Image info: %s', {k: v for k, v in image.info.items() if not isinstance(v, bytes)})

        # -- Step 6: Apply EXIF orientation ------------------------------------
        # Photos from phones often carry an EXIF orientation tag rather than
        # having their pixels pre-rotated.  exif_transpose() reads that tag,
        # rotates the pixel data to match, and drops the tag so downstream
        # code sees the correct orientation without needing to understand EXIF.
        image = ImageOps.exif_transpose(image) or image
        log.debug('EXIF orientation applied.  Effective dimensions: %dx%d.', image.width, image.height)

        # -- Step 7: Sanitise – strip all metadata -----------------------------
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

        # -- Step 8: Normalise colour mode -------------------------------------
        if image.mode not in ('RGB', 'RGBA'):
            log.debug('Converting image mode %s -> RGBA.', image.mode)
            image = image.convert('RGBA')

        steps.append({'step': t('step_validated'), 'status': 'success', 'detail': t('step_validated_detail')})
        log.info('Image loaded – mode=%s, size=%dx%d.', image.mode, image.width, image.height)

        # -- Step 9: Generate filename -----------------------------------------
        filename_base = generate_filename()
        steps.append({'step': t('step_filename'), 'status': 'success'})

        # -- Step 10: Resize & save --------------------------------------------
        urls = process_image(image, filename_base)
        total_bytes = sum(
            (AVATAR_ROOT / f'{size}x{size}' / f'{filename_base}.{fmt.lower()}').stat().st_size
            for size in img_cfg['sizes']
            for fmt in img_cfg['formats']
        )
        if total_bytes >= 1_048_576:
            size_label = f'{total_bytes / 1_048_576:.1f} MB'
        else:
            size_label = f'{total_bytes / 1024:.0f} KB'
        steps.append({'step': t('step_processed'), 'status': 'success', 'detail': t('step_processed_detail', sizes=len(img_cfg['sizes']), formats=len(img_cfg['formats']), total=size_label)})

        ak_size = ak_cfg.get('avatar_size', 1024)
        log.debug('Using %dx%d JPG for Authentik avatar URL (from authentik_api.avatar_size).', ak_size, ak_size)
        canonical_url = urls[f'{ak_size}x{ak_size}']['jpg']
        ad_thumb_size = ldap_cfg.get('thumbnail_size', 128)
        has_failure = False

        # -- Step 11: Update Authentik -----------------------------------------
        try:
            update_avatar_url(user['username'], canonical_url)
            status = 'dry-run' if dry_run else 'success'
            steps.append({'step': t('step_profile_synced'), 'status': status})
        except Exception as exc:
            log.exception('Failed to update Authentik avatar.')
            steps.append({'step': t('step_profile_synced'), 'status': 'failed'})
            has_failure = True

        # -- Step 12: Update AD (if enabled) -----------------------------------
        if ldap_is_enabled():
            try:
                thumb_path = AVATAR_ROOT / f'{ad_thumb_size}x{ad_thumb_size}' / f'{filename_base}.jpg'
                log.debug('Reading AD thumbnail from %s.', thumb_path)
                jpeg_bytes = thumb_path.read_bytes()
                update_ad_thumbnail(user['username'], jpeg_bytes)
                status = 'dry-run' if dry_run else 'success'
                steps.append({'step': t('step_ad_updated'), 'status': status})
            except Exception as exc:
                log.exception('Failed to update AD thumbnailPhoto.')
                steps.append({'step': t('step_ad_updated'), 'status': 'failed'})
                has_failure = True

        # -- Rollback saved files on any backend failure -----------------------
        if has_failure:
            log.warning('Backend update failed – cleaning up saved avatar files.')
            cleanup_avatar_files(filename_base)
            return jsonify({'steps': steps, 'error': 'Could not update your avatar. Please try again later.'}), 500

        # -- Save metadata JSON ------------------------------------------------
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
        return jsonify({'steps': steps, 'avatar_url': canonical_url}), 200

    except Exception as exc:
        log.exception('Upload processing failed.')
        steps.append({'step': t('step_processing_failed'), 'status': 'failed', 'detail': str(exc)})
        return jsonify({'steps': steps, 'error': str(exc)}), 500
