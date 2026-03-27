"""
imaging.py – Image processing helpers.

Handles secure, unguessable filename generation, resizing to all configured
square sizes, and saving in every configured format (jpg, png, webp).
"""

import json
import logging
from pathlib import Path
from time import time_ns
from uuid import uuid4
from secrets import token_urlsafe

from PIL import Image, ImageOps

from src.config import img_cfg, app_cfg

log = logging.getLogger('imaging')

# ---------------------------------------------------------------------------
# Security: Pillow decompression bomb limit
# ---------------------------------------------------------------------------
# A "decompression bomb" is a small file on disk (e.g. 1 MB) that expands to
# an enormous bitmap in memory (e.g. 20 GB) when decoded.  Pillow's default
# limit is ~178 megapixels, which is far too generous for an avatar uploader.
# 50 MP at 4 bytes/pixel = ~200 MB of RAM – a reasonable ceiling.
Image.MAX_IMAGE_PIXELS = 50_000_000

# Resolve the avatar storage root from config
AVATAR_ROOT = Path(app_cfg['avatar_storage_path'])

# Pre-compute frequently used values from config at import time
MAX_SIZE = max(img_cfg['sizes'])
_avatar_base_url = app_cfg['public_avatar_url']

# File extensions the upload endpoint will accept
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

# Pillow format strings we consider legitimate (what `image.format` returns)
ALLOWED_FORMATS = {'JPEG', 'PNG', 'WEBP'}

# Magic byte signatures for each allowed format.
# Checked before Pillow even touches the file so that crafted inputs with a
# wrong extension never reach the decoder.
MAGIC_SIGNATURES = {
    'JPEG': (0, b'\xFF\xD8\xFF'),           # SOI + first marker
    'PNG':  (0, b'\x89PNG\r\n\x1a\n'),      # 8-byte PNG header
    'WEBP_RIFF': (0, b'RIFF'),              # RIFF container …
    'WEBP_SIG':  (8, b'WEBP'),              # … with WEBP fourcc at offset 8
}

# Dimension guardrails
MIN_DIMENSION = 64    # Smaller than our smallest output is pointless
MAX_DIMENSION = 10000 # Sanity cap to avoid excessive memory/CPU use


def normalize_image(image: Image.Image) -> Image.Image:
    """
    Apply EXIF orientation, strip all metadata, and normalize the colour mode.

    Returns a clean pixel-only image in RGB or RGBA mode, ready for resizing.
    This is the shared preprocessing step used before any save/resize operation.
    
    Why:
      - EXIF can leak PII (GPS, device model, timestamps).
      - Ancillary PNG/JPEG chunks can carry hidden payloads.
      - ICC profiles are unnecessary for avatar thumbnails.
      - Starting from a clean image guarantees nothing unexpected passes
        through to the saved output files.
    """
    # Phone photos store orientation in EXIF rather than rotating pixels.
    # exif_transpose() reads that tag, rotates the pixel data to match, and 
    # drops the tag so downstream code sees the correct orientation without 
    # needing to understand EXIF.
    image = ImageOps.exif_transpose(image) or image
    log.debug('EXIF orientation applied. Effective dimensions: %dx%d.', image.width, image.height)

    # Rebuild from raw pixels — discards EXIF, ICC profiles, XMP, IPTC, and any
    # other ancillary chunks that could leak PII or carry hidden payloads.
    image = Image.frombytes(image.mode, image.size, image.tobytes())
    log.debug('Metadata stripped – working with clean pixel-only image.')

    if image.mode not in ('RGB', 'RGBA'):
        log.debug('Converting image mode %s -> RGBA.', image.mode)
        image = image.convert('RGBA')

    return image


def check_magic_bytes(raw_bytes: bytes) -> str | None:
    """
    Verify that `raw_bytes` start with a recognised image signature.

    Returns None on success or a human-readable error string on failure.
    This runs *before* Pillow's parser, acting as a first gate against files
    that are not real images (e.g. HTML, SVG, ZIP polyglots).
    """
    if len(raw_bytes) < 12:
        return 'File is too small to be a valid image (< 12 bytes).'

    is_jpeg = raw_bytes[:3] == MAGIC_SIGNATURES['JPEG'][1]
    is_png  = raw_bytes[:8] == MAGIC_SIGNATURES['PNG'][1]
    is_webp = raw_bytes[:4] == MAGIC_SIGNATURES['WEBP_RIFF'][1] and raw_bytes[8:12] == MAGIC_SIGNATURES['WEBP_SIG'][1]

    if not (is_jpeg or is_png or is_webp):
        return 'File does not start with a valid JPEG, PNG, or WebP signature.'
    return None


def generate_filename() -> str:
    """
    Build a filename that is practically impossible to guess.
    Format: `{uuid4_hex}-{token_urlsafe(64)}-{nanosecond_timestamp}`
    """
    name = f'{uuid4().hex}-{token_urlsafe(64)}-{time_ns()}'
    log.debug('Generated secure filename: %s', name)
    return name


def ensure_size_directories() -> None:
    """Create all size sub-directories under AVATAR_ROOT. Called once at startup."""
    for size in img_cfg['sizes']:
        (AVATAR_ROOT / f'{size}x{size}').mkdir(parents=True, exist_ok=True)
    log.debug('Ensured size directories under %s.', AVATAR_ROOT)


def process_image(image: Image.Image, filename_base: str) -> tuple[dict[str, dict[str, str]], int]:
    """
    Resize `image` to every configured square size and save in every configured format.

    Returns a tuple of:
      - nested dict: `{'WxH': {'ext': 'full_public_url', ...}, ...}`
      - total_bytes: combined size of all saved files
    """
    log.info('Starting image processing for %r.', filename_base)
    results: dict[str, dict[str, str]] = {}
    sizes = img_cfg['sizes']
    formats = img_cfg['formats']
    total_bytes = 0

    for size in sizes:
        key = f'{size}x{size}'
        log.debug('Resizing to %s using LANCZOS.', key)
        resized = image.resize((size, size), Image.LANCZOS)
        results[key] = {}

        size_dir = AVATAR_ROOT / key

        # Pre-convert RGB once per size if needed (for JPEG)
        resized_rgb = resized.convert('RGB') if resized.mode == 'RGBA' else resized

        for fmt in formats:
            ext = fmt.lower()
            out_path = size_dir / f'{filename_base}.{ext}'
            log.debug('Saving %s as %s.', key, ext.upper())

            if ext in ('jpg', 'jpeg'):
                resized_rgb.save(
                    out_path, format='JPEG', quality=img_cfg['jpeg_quality'], optimize=True,
                )
            elif ext == 'png':
                resized.save(out_path, format='PNG', compress_level=img_cfg['png_compress_level'], optimize=True)
            elif ext == 'webp':
                resized.save(out_path, format='WEBP', quality=img_cfg['webp_quality'], method=6)

            file_size = out_path.stat().st_size
            total_bytes += file_size
            results[key][ext] = f'{_avatar_base_url}/{key}/{filename_base}.{ext}'
            log.info('Saved %s/%s.%s (%s) – %d bytes.', key, filename_base, ext, ext.upper(), file_size)

    log.info('Image processing complete – %d sizes x %d formats, %d bytes total.', len(sizes), len(formats), total_bytes)
    return results, total_bytes


def cleanup_avatar_files(filename_base: str) -> None:
    """
    Remove all generated image files and the metadata JSON for one avatar set.

    Iterates every configured size × format combination and deletes the
    corresponding file.  Used both for rollback on upload failure and for
    retention / orphan cleanup.
    """
    log.debug('Cleaning up avatar files for %s.', filename_base)
    sizes = img_cfg['sizes']
    formats = img_cfg['formats']
    removed = 0
    for size in sizes:
        size_dir = AVATAR_ROOT / f'{size}x{size}'
        for fmt in formats:
            path = size_dir / f'{filename_base}.{fmt.lower()}'
            try:
                path.unlink(missing_ok=True)
                removed += 1
                log.debug('Deleted %s.', path)
            except OSError as exc:
                log.warning('Failed to remove %s during cleanup: %s', path, exc)
    # Also remove the metadata file if present
    meta_path = AVATAR_ROOT / f'{filename_base}.meta.json'
    try:
        meta_path.unlink(missing_ok=True)
        log.debug('Deleted metadata %s.', meta_path)
    except OSError:
        pass
    log.info('Cleanup: removed %d file(s) + metadata for %s.', removed, filename_base)


def cleanup_old_avatars(user_pk: int, keep: int) -> int:
    """
    Enforce per-user avatar retention by deleting the oldest uploads beyond
    the ``keep`` threshold.  Called after every successful upload.

    Matches metadata files by ``user_pk`` (the Authentik integer primary key),
    which is immutable even if the username is renamed.

    Returns the number of avatar sets removed.
    Does nothing if ``keep`` is 0 (unlimited retention).
    """
    if keep <= 0:
        log.debug('Retention cleanup skipped (keep=0, unlimited).')
        return 0

    log.debug('Retention check for user_pk=%s (keep=%d).', user_pk, keep)

    # Scan all metadata files and collect those belonging to this user.
    entries: list[tuple[str, str]] = []  # (uploaded_at, filename_base)
    for meta_path in AVATAR_ROOT.glob('*.meta.json'):
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            if meta.get('user_pk') == user_pk:
                entries.append((meta.get('uploaded_at', ''), meta['filename']))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            log.warning('Skipping unreadable metadata file %s: %s', meta_path, exc)

    log.debug('Found %d avatar set(s) for user_pk=%s.', len(entries), user_pk)

    if len(entries) <= keep:
        log.debug('Within retention limit – nothing to delete.')
        return 0

    # ISO 8601 timestamps sort lexicographically, so a simple string sort
    # gives us chronological order without parsing dates.
    entries.sort(key=lambda e: e[0], reverse=True)
    to_delete = entries[keep:]

    removed = 0
    for uploaded_at, filename_base in to_delete:
        log.info('Retention cleanup: removing avatar set %s (uploaded %s) for user_pk=%s.',
                 filename_base, uploaded_at, user_pk)
        cleanup_avatar_files(filename_base)
        removed += 1

    log.info('Retention cleanup for user_pk=%s: kept %d, removed %d avatar set(s).', user_pk, keep, removed)
    return removed


def get_all_avatar_metadata() -> list[dict]:
    """
    Read and return every .meta.json file from AVATAR_ROOT.

    Used by the orphan cleanup job to compare on-disk avatar ownership
    against the set of active Authentik users.
    """
    entries = []
    for meta_path in AVATAR_ROOT.glob('*.meta.json'):
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            entries.append(meta)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning('Skipping unreadable metadata file %s: %s', meta_path, exc)
    log.debug('Loaded %d metadata file(s) from %s.', len(entries), AVATAR_ROOT)
    return entries
