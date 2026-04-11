"""
imaging.py - Image processing helpers.

Handles secure, unguessable filename generation, resizing to all configured
square sizes, and saving in every configured format (jpg, png, webp).
"""

import io
import json
import logging
from pathlib import Path
from secrets import token_urlsafe
from time import time_ns
from uuid import uuid4

from PIL import Image, ImageOps

from src.config import app_cfg, img_cfg

log = logging.getLogger("imaging")

# Security: Pillow decompression bomb limit
# A "decompression bomb" is a small file on disk (e.g. 1 MB) that expands to
# an enormous bitmap in memory (e.g. 20 GB) when decoded.  Pillow's default
# limit is ~178 megapixels, which is far too generous for an avatar uploader.
# 25 MP at 4 bytes/pixel = ~100 MB of RAM - a practical ceiling that still
# accepts very high-resolution source photos while blocking crafted inputs.
Image.MAX_IMAGE_PIXELS = 25_000_000

# Resolve the avatar storage root from config
AVATAR_ROOT = Path(app_cfg.get("avatar_storage_path", "data/avatars"))

# Metadata JSON files live in a dedicated subfolder so they don't clutter
# the avatar root alongside the size subdirectories.
METADATA_ROOT = AVATAR_ROOT / "_metadata"

# Pre-compute frequently used values from config at import time
MAX_SIZE = max(img_cfg["sizes"])
_avatar_base_url = app_cfg["public_avatar_url"]

# File extensions the upload endpoint will accept
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Pillow format strings we consider legitimate (what `image.format` returns)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

# Magic byte signatures for each allowed format.
# Checked before Pillow even touches the file so that crafted inputs with a
# wrong extension never reach the decoder.
MAGIC_SIGNATURES = {
    "JPEG": (0, b"\xff\xd8\xff"),  # SOI + first marker
    "PNG": (0, b"\x89PNG\r\n\x1a\n"),  # 8-byte PNG header
    "WEBP_RIFF": (0, b"RIFF"),  # RIFF container …
    "WEBP_SIG": (8, b"WEBP"),  # … with WEBP fourcc at offset 8
}

# Dimension guardrails.
# MIN_DIMENSION is derived from the configured output sizes: uploading an image
# smaller than the smallest output size is pointless and is rejected early.
# If images.sizes changes, the floor moves with it automatically.
# MAX_DIMENSION caps each side independently at 8192 px (8 K).  For square
# images this limit fires first only when the side exceeds sqrt(MAX_IMAGE_PIXELS)
# ≈ 5000 px; above that Pillow's DecompressionBombError fires during decode
# instead.  Both paths are caught by the same except block in validate_upload.
MIN_DIMENSION = min(img_cfg["sizes"])
MAX_DIMENSION = 8192  # Each axis capped independently; Pillow caps total pixel area


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
    log.debug(
        "EXIF orientation applied. Effective dimensions: %dx%d.",
        image.width,
        image.height,
    )

    # Rebuild from raw pixels — discards EXIF, ICC profiles, XMP, IPTC, and any
    # other ancillary chunks that could leak PII or carry hidden payloads.
    image = Image.frombytes(image.mode, image.size, image.tobytes())
    log.debug("Metadata stripped - working with clean pixel-only image.")

    if image.mode not in ("RGB", "RGBA"):
        log.debug("Converting image mode %s -> RGBA.", image.mode)
        image = image.convert("RGBA")

    return image


def check_magic_bytes(raw_bytes: bytes) -> str | None:
    """
    Verify that `raw_bytes` start with a recognised image signature.

    Returns None on success or a human-readable error string on failure.
    This runs *before* Pillow's parser, acting as a first gate against files
    that are not real images (e.g. HTML, SVG, ZIP polyglots).
    """
    if len(raw_bytes) < 12:
        return "File is too small to be a valid image (< 12 bytes)."

    is_jpeg = raw_bytes[:3] == MAGIC_SIGNATURES["JPEG"][1]
    is_png = raw_bytes[:8] == MAGIC_SIGNATURES["PNG"][1]
    is_webp = (
        raw_bytes[:4] == MAGIC_SIGNATURES["WEBP_RIFF"][1]
        and raw_bytes[8:12] == MAGIC_SIGNATURES["WEBP_SIG"][1]
    )

    if not (is_jpeg or is_png or is_webp):
        return "File does not start with a valid JPEG, PNG, or WebP signature."
    return None


def generate_filename() -> str:
    """
    Build a filename that is practically impossible to guess.
    Format: `{uuid4_hex}-{token_urlsafe(64)}-{nanosecond_timestamp}`
    """
    name = f"{uuid4().hex}-{token_urlsafe(64)}-{time_ns()}"
    log.debug("Generated secure filename: %s", name)
    return name


def ensure_size_directories_existence() -> None:
    """Create all size sub-directories and the metadata directory under AVATAR_ROOT. Called once at startup."""
    for size in img_cfg["sizes"]:
        (AVATAR_ROOT / f"{size}x{size}").mkdir(parents=True, exist_ok=True)
    log.debug("Ensured size directories under %s.", AVATAR_ROOT)


# Shared format mapping and save helper
FORMAT_MAP = {
    "jpeg": ("JPEG", "jpg"),
    "jpg": ("JPEG", "jpg"),
    "png": ("PNG", "png"),
    "webp": ("WEBP", "webp"),
}

_QUALITY_STEP = 5
_MIN_QUALITY = 10


def _save_image(
    image: Image.Image, target, pillow_fmt: str, quality: int | None = None
) -> None:
    """Save *image* to *target* (file path or file-like) using format-specific settings."""
    if pillow_fmt == "JPEG":
        image.save(
            target,
            format="JPEG",
            quality=quality if quality is not None else img_cfg["jpeg_quality"],
            optimize=True,
        )
    elif pillow_fmt == "PNG":
        image.save(
            target,
            format="PNG",
            compress_level=img_cfg["png_compress_level"],
            optimize=True,
        )
    elif pillow_fmt == "WEBP":
        image.save(
            target,
            format="WEBP",
            quality=quality if quality is not None else img_cfg["webp_quality"],
            method=6,
        )
    else:
        raise ValueError(f"Unsupported Pillow format: {pillow_fmt!r}")


def process_image(
    image: Image.Image, filename_base: str
) -> tuple[dict[str, dict[str, str]], int]:
    """
    Resize `image` to every configured square size and save in every configured format.

    Returns a tuple of:
      - nested dict: `{'WxH': {'ext': 'full_public_url', ...}, ...}`
      - total_bytes: combined size of all saved files
    """
    log.info("Starting image processing for %r.", filename_base)
    results: dict[str, dict[str, str]] = {}
    sizes = img_cfg["sizes"]
    formats = img_cfg["formats"]
    total_bytes = 0

    for size in sizes:
        key = f"{size}x{size}"
        log.debug("Resizing to %s using LANCZOS.", key)
        resized = image.resize((size, size), Image.LANCZOS)
        results[key] = {}

        size_dir = AVATAR_ROOT / key

        # Pre-convert RGB once per size if needed (for JPEG)
        resized_rgb = resized.convert("RGB") if resized.mode == "RGBA" else resized

        for fmt in formats:
            ext = fmt.lower()
            pillow_fmt = FORMAT_MAP[ext][0]
            out_path = size_dir / f"{filename_base}.{ext}"
            log.debug("Saving %s as %s.", key, ext.upper())

            target_img = resized_rgb if pillow_fmt == "JPEG" else resized
            _save_image(target_img, out_path, pillow_fmt)

            file_size = out_path.stat().st_size
            total_bytes += file_size
            results[key][ext] = f"{_avatar_base_url}/{key}/{filename_base}.{ext}"
            log.debug(
                "Saved %s/%s.%s (%s) - %d bytes.",
                key,
                filename_base,
                ext,
                ext.upper(),
                file_size,
            )

    log.info(
        "Image processing complete - %d sizes x %d formats, %d bytes total. Filename: %s",
        len(sizes),
        len(formats),
        total_bytes,
        filename_base,
    )
    return results, total_bytes


# LDAP image preparation


def prepare_ldap_image(
    source_image: Image.Image,
    filename_base: str,
    target_size: int,
    image_type: str,
    max_file_size_kb: int,
) -> bytes:
    """
    Prepare image bytes for an LDAP binary attribute.

    Reuses a pre-generated file if it exists at the exact size/format and fits
    within the file size limit.  Otherwise, resizes from the source image and
    reduces quality iteratively until the output fits.

    Returns encoded image bytes ready for LDAP.
    Raises ValueError if the image cannot be compressed to fit.
    """
    pillow_fmt, file_ext = FORMAT_MAP[image_type.lower()]
    max_bytes = max_file_size_kb * 1024 if max_file_size_kb > 0 else 0

    log.debug(
        "Preparing LDAP image: %dx%d %s (max %d KB).",
        target_size,
        target_size,
        pillow_fmt,
        max_file_size_kb,
    )

    # Try to reuse a pre-generated file if it exists and fits the size limit
    existing_path = (
        AVATAR_ROOT / f"{target_size}x{target_size}" / f"{filename_base}.{file_ext}"
    )
    try:
        data = existing_path.read_bytes()
        if max_bytes == 0 or len(data) <= max_bytes:
            log.info(
                "Reusing pre-generated %s (%d bytes) for LDAP.",
                existing_path.name,
                len(data),
            )
            return data
        log.debug(
            "Pre-generated file %s is %d bytes, exceeds limit of %d bytes - will re-encode.",
            existing_path.name,
            len(data),
            max_bytes,
        )
    except FileNotFoundError:
        pass

    # Resize from source image and encode to target format
    log.debug(
        "Resizing to %dx%d %s for LDAP attribute.", target_size, target_size, pillow_fmt
    )
    resized = source_image.resize((target_size, target_size), Image.LANCZOS)
    if pillow_fmt == "JPEG" and resized.mode != "RGB":
        resized = resized.convert("RGB")

    # PNG is lossless (quality=None); JPEG/WebP use configured quality
    if pillow_fmt == "JPEG":
        quality = img_cfg["jpeg_quality"]
    elif pillow_fmt == "WEBP":
        quality = img_cfg["webp_quality"]
    else:
        quality = None

    def _encode(q=None):
        buf = io.BytesIO()
        _save_image(resized, buf, pillow_fmt, quality=q)
        return buf.getvalue()

    data = _encode(quality)
    log.debug(
        "Encoded %dx%d %s: %d bytes (quality=%s).",
        target_size,
        target_size,
        pillow_fmt,
        len(data),
        quality,
    )

    if max_bytes == 0 or len(data) <= max_bytes:
        log.info(
            "LDAP image ready: %dx%d %s, %d bytes (quality=%s).",
            target_size,
            target_size,
            pillow_fmt,
            len(data),
            quality,
        )
        return data

    # Quality reduction loop (JPEG / WebP only)
    if quality is None:
        raise ValueError(
            f"PNG image at {target_size}x{target_size} is {len(data)} bytes, "
            f"exceeding the {max_file_size_kb} KB limit. PNG is lossless and quality "
            f"cannot be reduced. Use JPEG or WebP, or increase max_file_size."
        )

    log.debug(
        "Image exceeds %d KB limit - starting quality reduction from %d.",
        max_file_size_kb,
        quality,
    )
    while quality > _MIN_QUALITY:
        quality = max(quality - _QUALITY_STEP, _MIN_QUALITY)
        data = _encode(quality)
        log.debug("Re-encoded at quality=%d: %d bytes.", quality, len(data))
        if len(data) <= max_bytes:
            log.info(
                "LDAP image fits at quality=%d: %dx%d %s, %d bytes (limit %d KB).",
                quality,
                target_size,
                target_size,
                pillow_fmt,
                len(data),
                max_file_size_kb,
            )
            return data

    raise ValueError(
        f"{pillow_fmt} image at {target_size}x{target_size} is still {len(data)} bytes "
        f"at quality={_MIN_QUALITY}, exceeding the {max_file_size_kb} KB limit."
    )


def cleanup_avatar_files(filename_base: str) -> tuple[int, int]:
    """
    Remove all generated image files and the metadata JSON for one avatar set.

    Iterates every configured size × format combination and deletes the
    corresponding file.  Used both for rollback on upload failure and for
    retention cleanup.

    Returns (deleted, failed): files successfully removed and files that raised
    an OSError.  Files that simply do not exist are silently skipped (not
    counted as failures).
    """
    log.info("Cleaning up avatar files for %s.", filename_base)
    sizes = img_cfg["sizes"]
    formats = img_cfg["formats"]
    deleted = 0
    failed = 0
    for size in sizes:
        size_dir = AVATAR_ROOT / f"{size}x{size}"
        for fmt in formats:
            path = size_dir / f"{filename_base}.{fmt.lower()}"
            try:
                path.unlink()
                deleted += 1
                log.debug("Deleted %s.", path)
            except FileNotFoundError:
                pass  # already gone - not a failure
            except OSError as exc:
                log.warning("Failed to remove %s during cleanup: %s", path, exc)
                failed += 1
    # Also remove the metadata file if present
    meta_path = METADATA_ROOT / f"{filename_base}.meta.json"
    try:
        meta_path.unlink()
        deleted += 1
        log.debug("Deleted metadata %s.", meta_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Failed to remove metadata %s during cleanup: %s", meta_path, exc)
        failed += 1
    log.info("Cleanup: %d deleted, %d failed for %s.", deleted, failed, filename_base)
    return deleted, failed


def get_all_avatar_metadata() -> list[dict]:
    """
    Read and return every .meta.json file from AVATAR_ROOT.

    Used by the cleanup job to compare on-disk avatar ownership
    against the set of active Authentik users.
    """
    entries = []
    for meta_path in METADATA_ROOT.glob("*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            entries.append(meta)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Skipping unreadable metadata file %s: %s", meta_path, exc)
    log.debug("Loaded %d metadata file(s) from %s.", len(entries), METADATA_ROOT)
    return entries
