"""
image_validation.py - Uploaded image validation.

Performs all synchronous checks on an uploaded file before it enters the
processing pipeline:
  1. Filename and extension allow-list
  2. Magic byte verification (detects fake extensions)
  3. Pillow decode (catches corrupt / truncated files)
  4. Decoded format allow-list (rejects formats Pillow accepts but we do not)
  5. Dimension bounds (min size, max size per axis)

Keeping validation separate from upload.py allows routes.py to import only
what it needs without pulling in the entire SSE pipeline.
"""

import io
import logging

from PIL import Image

from src.config import img_cfg
from src.i18n import t
from src.image_formats import ALLOWED_EXTENSIONS, ALLOWED_FORMATS

log = logging.getLogger("upload")

# Security: Pillow decompression bomb limit.
# A "decompression bomb" is a small file on disk that expands to an enormous
# bitmap in memory when decoded.  25 MP at 4 bytes/pixel ≈ 100 MB of RAM - a
# practical ceiling that accepts very high-resolution source photos while
# blocking crafted inputs.  Set here (at the validation boundary) so the limit
# is in place before any Image.open() call in validate_upload().
Image.MAX_IMAGE_PIXELS = 25_000_000

# Magic byte signatures for each allowed upload format.
# Checked before Pillow touches the file so that crafted inputs with a fake
# extension never reach the decoder.
MAGIC_SIGNATURES = {
    "JPEG": (0, b"\xff\xd8\xff"),  # SOI + first marker
    "PNG": (0, b"\x89PNG\r\n\x1a\n"),  # 8-byte PNG header
    "WEBP_RIFF": (0, b"RIFF"),  # RIFF container ...
    "WEBP_SIG": (8, b"WEBP"),  # ... with WEBP fourcc at offset 8
    "AVIF_FTYP": (4, b"ftyp"),  # ISO Base Media file type box (AVIF/HEIF/MP4)
    "AVIF_BRAND": (8, b"avif"),  # AVIF major brand ("avis" also valid for sequences)
}

# Dimension guardrails.
# MIN_DIMENSION is the smallest configured output size - uploading an image
# smaller than the smallest output is pointless and rejected early.
# If images.sizes changes, the floor moves with it automatically.
# MAX_DIMENSION caps each axis independently at 8 192 px (8 K).  For square
# images this limit fires first only when the side exceeds sqrt(MAX_IMAGE_PIXELS)
# ≈ 5 000 px; above that Pillow's DecompressionBombError fires during decode.
# Both paths are caught by the same except block in validate_upload().
MIN_DIMENSION = min(img_cfg["sizes"])
MAX_DIMENSION = 8192  # Each axis capped independently; Pillow caps total pixel area


def check_magic_bytes(raw_bytes: bytes) -> str | None:
    """
    Verify that raw_bytes start with a recognized image signature.

    Returns None on success or a human-readable error string on failure.
    This runs before Pillow's parser, acting as a first gate against files
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
    # AVIF is an ISO Base Media file: bytes 4-7 are "ftyp", bytes 8-11 are the
    # major brand.  "avif" covers still images; "avis" covers AVIF image sequences.
    is_avif = raw_bytes[4:8] == b"ftyp" and raw_bytes[8:12] in (b"avif", b"avis")

    if not (is_jpeg or is_png or is_webp or is_avif):
        return "File does not start with a valid JPEG, PNG, WebP, or AVIF signature."
    return None


class ValidationError(Exception):
    """Raised when the uploaded file fails a validation check."""


def validate_upload(file) -> Image.Image:
    """
    Run all validation checks on the uploaded file and return the decoded PIL
    Image.  Normalization (EXIF orientation, metadata stripping, color mode)
    is deferred to the SSE pipeline so the client sees granular progress.

    Raises ``ValidationError`` with a user-facing message on failure.
    """
    # Filename & extension check
    if not file.filename:
        raise ValidationError(t("error.empty_filename"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            t(
                "error.invalid_type",
                ext=ext,
                accepted=", ".join(sorted(ALLOWED_EXTENSIONS)),
            )
        )

    # Read raw bytes and verify magic signature
    raw_bytes = file.read()
    if not raw_bytes:
        raise ValidationError(t("error.empty_file"))

    # Check magic bytes to prevent fake extensions (e.g. .jpg that is actually a .exe).
    # magic_err holds a technical description kept for the log - the user sees a
    # generic translated message that does not expose internal format details.
    magic_err = check_magic_bytes(raw_bytes)
    if magic_err:
        log.warning("Magic byte check failed: %s", magic_err)
        raise ValidationError(t("error.invalid_signature"))
    log.debug("Magic-byte signature check passed.")

    # Decode with Pillow and verify format
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        # .load() forces full pixel decoding - catches truncated/corrupt files
        # that Image.open() (header-only) would not detect.
        image.load()
    except Exception as exc:
        log.warning("Pillow could not decode image: %s", exc)
        raise ValidationError(t("error.decode_failed")) from exc

    # Only allow formats we explicitly intend to handle (a .png could decode
    # as TIFF if Pillow recognizes the actual content).
    if image.format not in ALLOWED_FORMATS:
        log.warning("Decoded image format %r is not in the allow-list.", image.format)
        raise ValidationError(t("error.format_not_allowed"))
    log.debug("Decoded format %s is in the allow-list.", image.format)

    # Dimension checks
    w, h = image.size
    if w < MIN_DIMENSION or h < MIN_DIMENSION:
        raise ValidationError(t("error.too_small", w=w, h=h, min_dim=MIN_DIMENSION))
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise ValidationError(t("error.too_large", w=w, h=h, max_dim=MAX_DIMENSION))

    log.info(
        "Upload accepted: content_type=%r, size=%d bytes, %dx%d, mode=%s, format=%s.",
        file.content_type,
        len(raw_bytes),
        w,
        h,
        image.mode,
        image.format,
    )

    return image
