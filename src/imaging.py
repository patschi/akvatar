"""
imaging.py – Image processing helpers.

Handles secure, unguessable filename generation, resizing to all configured
square sizes, and saving in every configured format (jpg, png, webp).
"""

import logging
from pathlib import Path
from time import time_ns
from uuid import uuid4
from secrets import token_urlsafe

from PIL import Image

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


def process_image(image: Image.Image, filename_base: str) -> dict[str, dict[str, str]]:
    """
    Resize `image` to every configured square size and save in every configured format.

    Returns a nested dict: `{'WxH': {'ext': 'full_public_url', ...}, ...}`
    """
    log.info('Starting image processing for %r.', filename_base)
    results: dict[str, dict[str, str]] = {}
    sizes = img_cfg['sizes']
    formats = img_cfg['formats']
    avatar_base_url = app_cfg['public_avatar_url']

    for size in sizes:
        key = f'{size}x{size}'
        log.debug('Resizing to %s using LANCZOS.', key)
        resized = image.resize((size, size), Image.LANCZOS)
        results[key] = {}

        # Ensure the size sub-directory exists
        size_dir = AVATAR_ROOT / key
        size_dir.mkdir(parents=True, exist_ok=True)

        for fmt in formats:
            ext = fmt.lower()
            out_path = size_dir / f'{filename_base}.{ext}'
            log.debug('Saving %s as %s.', key, ext.upper())

            if ext in ('jpg', 'jpeg'):
                # JPEG cannot store alpha – drop it
                (resized.convert('RGB') if resized.mode == 'RGBA' else resized).save(
                    out_path, format='JPEG', quality=img_cfg['jpeg_quality'], optimize=True,
                )
            elif ext == 'png':
                resized.save(out_path, format='PNG', compress_level=img_cfg['png_compress_level'], optimize=True)
            elif ext == 'webp':
                resized.save(out_path, format='WEBP', quality=img_cfg['webp_quality'], method=6)

            results[key][ext] = f'{avatar_base_url}/{key}/{filename_base}.{ext}'
            log.info('Saved %s/%s.%s (%s) – %d bytes.', key, filename_base, ext, ext.upper(), out_path.stat().st_size)

    log.info('Image processing complete – %d sizes x %d formats.', len(sizes), len(formats))
    return results


def cleanup_avatar_files(filename_base: str) -> None:
    """Remove all generated avatar files for the given filename base (used on rollback after a backend failure)."""
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
            except OSError as exc:
                log.warning('Failed to remove %s during cleanup: %s', path, exc)
    # Also remove the metadata file if present
    meta_path = AVATAR_ROOT / f'{filename_base}.meta.json'
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass
    log.info('Cleanup: removed %d avatar files for %s.', removed, filename_base)
