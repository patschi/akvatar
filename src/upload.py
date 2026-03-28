"""
upload.py – Avatar upload processing pipeline.

Handles the full lifecycle of an avatar upload:
  1. Validate the uploaded file (extension, magic bytes, Pillow decode, dimensions)
  2. Stream processing progress to the client as Server-Sent Events (SSE):
     - Generate images in all configured sizes/formats
     - Push the canonical avatar URL to Authentik
     - Update LDAP photo attributes (if applicable)
     - Persist metadata JSON to disk

Each SSE step reports success/failure independently so the frontend can show
granular progress.  On backend failure the generated files are cleaned up.

Pipeline steps that yield SSE frames *and* produce a result use Python's
generator-return convention: they ``yield`` SSE strings and ``return`` data.
The orchestrator collects results via ``yield from``.
"""

import io
import json
import logging
from datetime import datetime, timezone

from PIL import Image

from src.config import img_cfg, ak_cfg, dry_run
from src.i18n import t
from src.imaging import (
    METADATA_ROOT, ALLOWED_EXTENSIONS, ALLOWED_FORMATS, MIN_DIMENSION, MAX_DIMENSION,
    _FORMAT_MAP, normalize_image, check_magic_bytes, generate_filename,
    process_image, cleanup_avatar_files, prepare_ldap_image,
)
from src.authentik_api import update_avatar_url
from src.ldap_client import (
    update_photos as update_ldap_photos,
    is_enabled as ldap_is_enabled,
    get_photos_config as ldap_photos_config,
)

log = logging.getLogger('upload')

# ---------------------------------------------------------------------------
# Module-level config (immutable after startup)
# ---------------------------------------------------------------------------
_ldap_enabled = ldap_is_enabled()
_ldap_photos = ldap_photos_config()
_ak_avatar_size = ak_cfg.get('avatar_size', 1024)


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as a single Server-Sent Event frame."""
    return f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Upload validation (synchronous – called before switching to SSE stream)
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when the uploaded file fails a validation check."""


def validate_upload(file) -> Image.Image:
    """
    Run all validation checks on the uploaded file and return a normalised
    PIL Image ready for processing.

    Raises ``ValidationError`` with a user-facing message on failure.
    """
    # -- Filename & extension --------------------------------------------------
    if not file.filename:
        raise ValidationError('Empty filename.')

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f'File type .{ext} not allowed. Accepted: {", ".join(sorted(ALLOWED_EXTENSIONS))}'
        )

    # -- Read bytes & magic signature ------------------------------------------
    raw_bytes = file.read()
    if not raw_bytes:
        raise ValidationError('Uploaded file is empty.')

    # Check magic bytes to prevent fake extensions (e.g. .jpg that is actually a .exe).
    magic_err = check_magic_bytes(raw_bytes)
    if magic_err:
        log.warning('Magic byte check failed: %s', magic_err)
        raise ValidationError(magic_err)
    log.debug('Magic-byte signature check passed.')

    # -- Pillow decode ---------------------------------------------------------
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        # .load() forces full pixel decoding — catches truncated/corrupt files
        # that Image.open() (header-only) would not detect.
        image.load()
    except Exception as exc:
        log.warning('Pillow could not decode image: %s', exc)
        raise ValidationError(f'Cannot decode image: {exc}') from exc

    # Only allow formats we explicitly intend to handle (a .png could decode
    # as TIFF if Pillow recognises the actual content).
    if image.format not in ALLOWED_FORMATS:
        raise ValidationError(f'Image format {image.format!r} is not allowed.')
    log.debug('Decoded format %s is in the allow-list.', image.format)

    # -- Dimension checks ------------------------------------------------------
    w, h = image.size
    if w < MIN_DIMENSION or h < MIN_DIMENSION:
        raise ValidationError(
            f'Image is too small ({w}x{h}). Minimum dimension is {MIN_DIMENSION}px.'
        )
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise ValidationError(
            f'Image is too large ({w}x{h}). Maximum dimension is {MAX_DIMENSION}px.'
        )

    log.info(
        'Upload accepted: content_type=%r, size=%d bytes, %dx%d, mode=%s, format=%s.',
        file.content_type, len(raw_bytes), w, h, image.mode, image.format,
    )

    # Normalise: apply EXIF orientation, strip metadata, ensure RGB(A)
    return normalize_image(image)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
# Steps that need to both yield SSE frames and return data use Python's
# generator-return: yield SSE strings, return the result.  The caller
# collects via `result = yield from step(...)`.

def _step_process_image(image: Image.Image, filename_base: str):
    """Resize & save the image in all configured sizes/formats."""
    urls, total_bytes = process_image(image, filename_base)
    if not urls:
        raise RuntimeError('Image processing produced no output – check images.sizes/formats config.')

    if total_bytes >= 1_048_576:
        size_label = f'{total_bytes / 1_048_576:.1f} MB'
    else:
        size_label = f'{total_bytes / 1024:.0f} KB'

    yield _sse({
        'step': t('step_processed'), 'status': 'success',
        'detail': t('step_processed_detail',
                     sizes=len(img_cfg['sizes']),
                     formats=len(img_cfg['formats']),
                     total=size_label),
    })
    return urls, total_bytes


def _resolve_canonical_url(urls: dict) -> str:
    """
    Look up the canonical avatar URL (used by Authentik) from the generated
    URL map.  Raises RuntimeError if the expected size/format is missing.
    """
    size_key = f'{_ak_avatar_size}x{_ak_avatar_size}'
    canonical = urls.get(size_key, {}).get('jpg')
    if not canonical:
        raise RuntimeError(
            f'Canonical avatar URL not found: size={size_key}, format=jpg. '
            f'Ensure {_ak_avatar_size} is in images.sizes and "jpg" is in images.formats.'
        )
    log.debug('Canonical Authentik avatar URL: %s', canonical)
    return canonical


def _step_sync_authentik(user_pk: int, canonical_url: str):
    """
    Push the avatar URL to Authentik via API.

    Yields one SSE frame.  Returns ``(ak_attrs, failed)``.
    On failure the pipeline continues so LDAP can be skipped gracefully.
    """
    try:
        ak_attrs = update_avatar_url(user_pk, canonical_url)
        if not isinstance(ak_attrs, dict):
            raise TypeError(f'Authentik API returned {type(ak_attrs).__name__} instead of dict.')
        yield _sse({'step': t('step_profile_synced'), 'status': 'dry-run' if dry_run else 'success'})
        return ak_attrs, False
    except Exception:
        log.exception('Failed to update Authentik avatar for pk=%s.', user_pk)
        yield _sse({'step': t('step_profile_synced'), 'status': 'failed'})
        return {}, True


def _build_ldap_updates(image: Image.Image, urls: dict, filename_base: str) -> list[dict]:
    """
    Build LDAP attribute updates from the ``ldap.photos`` config.

    For ``binary`` entries the image is encoded on-the-fly (or reused from disk).
    For ``url`` entries the pre-generated public URL is looked up.
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


def _step_sync_ldap(image: Image.Image, urls: dict, filename_base: str,
                    ak_attrs: dict, user_pk: int):
    """
    Update LDAP photo attributes if applicable.

    Yields SSE frames.  Returns True on failure, False on success/skip.
    Skips silently when LDAP is disabled or the user has no ``ldap_uniq``.
    """
    if not (_ldap_enabled and _ldap_photos):
        return False

    ldap_uniq = ak_attrs.get('ldap_uniq')

    # Users without ldap_uniq are Authentik-only (not synced from LDAP)
    if not ldap_uniq:
        log.info('User pk=%s has no ldap_uniq – skipping LDAP updates.', user_pk)
        yield _sse({'step': t('step_ldap_updated'), 'status': 'skipped'})
        return False

    log.debug('User has ldap_uniq=%r – preparing %d LDAP photo update(s).', ldap_uniq, len(_ldap_photos))
    try:
        ldap_updates = _build_ldap_updates(image, urls, filename_base)
        update_ldap_photos(ldap_uniq, ldap_updates)
        detail = ', '.join(u['attribute'] for u in ldap_updates)
        yield _sse({'step': t('step_ldap_updated'), 'status': 'dry-run' if dry_run else 'success', 'detail': detail})
        return False
    except Exception:
        log.exception('Failed to update LDAP for ldap_uniq=%s.', ldap_uniq)
        yield _sse({'step': t('step_ldap_updated'), 'status': 'failed'})
        return True


def _save_metadata(filename_base: str, user_pk: int, total_bytes: int) -> None:
    """
    Persist upload metadata as JSON.  Uses the Authentik PK (immutable, no PII)
    as the owner identifier for cleanup/retention matching.
    """
    metadata = {
        'filename': filename_base,
        'user_pk': user_pk,
        'uploaded_at': datetime.now(timezone.utc).isoformat(),
        'sizes': img_cfg['sizes'],
        'formats': img_cfg['formats'],
        'total_bytes': total_bytes,
    }
    meta_path = METADATA_ROOT / f'{filename_base}.meta.json'
    meta_path.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    log.debug('Metadata saved to %s.', meta_path)


# ---------------------------------------------------------------------------
# Main SSE generator – orchestrates the full pipeline
# ---------------------------------------------------------------------------

def generate_sse(user: dict, image: Image.Image):
    """
    Generator that drives the upload pipeline and yields SSE frames.

    ``user`` is the session user dict (must contain ``pk`` and ``username``).
    ``image`` is the already-validated and normalised PIL Image.
    """
    username = user['username']
    user_pk = user['pk']
    filename_base = None

    try:
        yield _sse({'step': t('step_validated'), 'status': 'success', 'detail': t('step_validated_detail')})

        # -- Step 1: Generate secure filename ----------------------------------
        filename_base = generate_filename()
        yield _sse({'step': t('step_filename'), 'status': 'success'})

        # -- Step 2: Resize & save all configured sizes/formats ----------------
        urls, total_bytes = yield from _step_process_image(image, filename_base)

        # -- Step 3: Resolve canonical avatar URL for Authentik ----------------
        canonical_url = _resolve_canonical_url(urls)

        # -- Step 4: Push avatar URL to Authentik ------------------------------
        ak_attrs, ak_failed = yield from _step_sync_authentik(user_pk, canonical_url)

        # -- Step 5: Update LDAP photo attributes (if applicable) --------------
        ldap_failed = yield from _step_sync_ldap(image, urls, filename_base, ak_attrs, user_pk)

        # -- Rollback on any backend failure -----------------------------------
        if ak_failed or ldap_failed:
            log.warning('Backend update failed – rolling back avatar files for %s.', filename_base)
            cleanup_avatar_files(filename_base)
            yield _sse({'done': True, 'error': 'Could not update your avatar. Please try again later.'})
            return

        # -- Step 6: Persist metadata ------------------------------------------
        _save_metadata(filename_base, user_pk, total_bytes)

        log.info('Upload pipeline complete for user %r (pk=%s).', username, user_pk)
        yield _sse({'done': True, 'avatar_url': canonical_url})

    except Exception as exc:
        log.exception('Upload processing failed for user %r.', username)
        if filename_base:
            cleanup_avatar_files(filename_base)
        yield _sse({'step': t('step_processing_failed'), 'status': 'failed', 'detail': str(exc)})
        yield _sse({'done': True, 'error': str(exc)})
