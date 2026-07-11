"""
imaging.py - Image processing helpers.

Handles secure, unguessable filename generation, resizing to all configured
square sizes, and saving in every configured format (jpg, png, webp, avif).
"""

import io
import json
import logging
from collections import defaultdict
from pathlib import Path
from secrets import token_urlsafe
from time import time_ns
from uuid import uuid4

from PIL import Image, ImageOps

from src.config import (
    avatar_storage_path,
    dry_run,
    img_avif_quality,
    img_formats,
    img_jpeg_quality,
    img_png_compress_level,
    img_rgba_background_color,
    img_sizes,
    img_webp_quality,
    public_avatar_url,
)
from src.image_formats import BACKFILL_SOURCE_PREFERENCE, FORMAT_MAP

log = logging.getLogger("imaging")

# Resolve the avatar storage root from config
AVATAR_ROOT = Path(avatar_storage_path)

# Metadata JSON files live in a dedicated subfolder so they don't clutter
# the avatar root alongside the size subdirectories.
METADATA_ROOT = AVATAR_ROOT / "_metadata"

# Pre-compute frequently used values from config at import time
MAX_SIZE = max(img_sizes)
AVATAR_BASE_URL = public_avatar_url

# Background color used when compositing RGBA images onto a solid fill before
# encoding to JPEG (which has no alpha channel).  Without compositing, transparent
# and semi-transparent pixels would map to black - compositing onto a configured
# color produces the correct result for logos and photos with transparent borders.
# Configured via images.rgba_background_color; defaults to white [255, 255, 255].
_RGBA_BG_COLOR: tuple[int, int, int] = img_rgba_background_color


def _flatten_rgba_to_rgb(image: Image.Image) -> Image.Image:
    """
    Composite an RGBA image onto ``_RGBA_BG_COLOR`` and return an RGB image.
    No-op when the input is already RGB.

    Used before any JPEG encode (JPEG has no alpha channel).  A bare
    ``.convert("RGB")`` would map transparent pixels to black; compositing
    onto the configured background color produces the intended result for
    logos/photos with transparent borders.

    Assumes the input has already been through ``normalize_image()`` and is
    therefore in either RGB or RGBA mode.  Any other mode is a contract
    violation and raises ``ValueError`` immediately - this turns a previously
    silent fallback (which produced black backgrounds for unrecognized modes)
    into a loud failure that surfaces the bug at its source.
    """
    if image.mode == "RGB":
        return image
    if image.mode == "RGBA":
        bg = Image.new("RGB", image.size, _RGBA_BG_COLOR)
        # Use the alpha band as the paste mask.  getchannel("A") fetches only
        # the alpha band, avoiding the work of splitting all four RGBA bands.
        bg.paste(image, mask=image.getchannel("A"))
        return bg
    raise ValueError(
        f"Unexpected image mode {image.mode!r} - "
        "normalize_image() should produce RGB or RGBA only."
    )


def normalize_image(image: Image.Image) -> Image.Image:
    """
    Apply EXIF orientation, strip all metadata, and normalize the color mode.

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

    # Rebuild from raw pixels - discards EXIF, ICC profiles, XMP, IPTC, and any
    # other ancillary chunks that could leak PII or carry hidden payloads.
    image = Image.frombytes(image.mode, image.size, image.tobytes())
    log.debug("Metadata stripped - working with clean pixel-only image.")

    if image.mode not in ("RGB", "RGBA"):
        log.debug("Converting image mode %s -> RGBA.", image.mode)
        image = image.convert("RGBA")

    return image


def generate_filename() -> str:
    """
    Build a filename that is practically impossible to guess.
    Format: `{uuid4_hex}-{token_urlsafe(64)}-{nanosecond_timestamp}`
    """
    name = f"{uuid4().hex}-{token_urlsafe(64)}-{time_ns()}"
    log.debug("Generated secure filename: %s", name)
    return name


def ensure_size_directories_existence() -> None:
    """Create AVATAR_ROOT, all size sub-directories, and the metadata directory. Called once at startup."""
    AVATAR_ROOT.mkdir(parents=True, exist_ok=True)
    for size in img_sizes:
        (AVATAR_ROOT / f"{size}x{size}").mkdir(parents=True, exist_ok=True)
    METADATA_ROOT.mkdir(parents=True, exist_ok=True)
    log.debug("Ensured size and metadata directories under %s.", AVATAR_ROOT)


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
            quality=quality if quality is not None else img_jpeg_quality,
            optimize=True,
        )
    elif pillow_fmt == "PNG":
        image.save(
            target,
            format="PNG",
            compress_level=img_png_compress_level,
            optimize=True,
        )
    elif pillow_fmt == "WEBP":
        image.save(
            target,
            format="WEBP",
            quality=quality if quality is not None else img_webp_quality,
            method=6,
        )
    elif pillow_fmt == "AVIF":
        # AVIF supports alpha natively, so RGBA images are passed as-is.
        # quality follows the same 0-100 scale as JPEG/WebP.
        image.save(
            target,
            format="AVIF",
            quality=quality if quality is not None else img_avif_quality,
        )
    else:
        raise ValueError(f"Unsupported Pillow format: {pillow_fmt!r}")


class _ResizedAvatar:
    """
    Wraps one resized avatar image and returns the correct pixel buffer per
    output format, flattening RGBA -> RGB exactly once (lazily) and reusing it
    across every JPEG output of this size.

    Centralizes the format -> image decision shared by process_image() (fresh
    uploads) and backfill_avatar_set() (regenerating missing files) so the two
    code paths cannot drift in how they handle alpha and JPEG flattening.
    """

    def __init__(self, resized: Image.Image) -> None:
        self._resized = resized
        self._rgb: Image.Image | None = None

    def for_format(self, pillow_fmt: str) -> Image.Image:
        """
        Return the image to encode for *pillow_fmt*: a flattened RGB copy for
        JPEG (which has no alpha channel), or the resized image as-is for
        formats that support alpha (PNG/WebP/AVIF).
        """
        if pillow_fmt != "JPEG":
            return self._resized
        if self._rgb is None:
            self._rgb = _flatten_rgba_to_rgb(self._resized)
        return self._rgb


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
    sizes = img_sizes
    formats = img_formats
    total_bytes = 0

    # Pre-create the results dict in the original (config) order so the
    # returned mapping iterates in the same order as `images.sizes` regardless
    # of the descending resize order used below.
    results: dict[str, dict[str, str]] = {f"{s}x{s}": {} for s in sizes}

    # Resize from largest to smallest, feeding each step's output into the
    # next.  Chained downscale runs LANCZOS over fewer pixels at each step
    # instead of resampling from the full source for every size, which is
    # roughly 3-5x less pixel work for typical (e.g. 1024/512/256/128/64)
    # configurations.  Output quality is comparable for the typical case
    # where the source is significantly larger than the largest output size.
    sizes_desc = sorted(sizes, reverse=True)
    current = image  # source for the next downscale step

    for size in sizes_desc:
        key = f"{size}x{size}"
        log.debug("Resizing to %s using LANCZOS.", key)
        resized = current.resize((size, size), Image.LANCZOS)
        current = resized  # next iteration downscales from this result

        size_dir = AVATAR_ROOT / key

        # Shared renderer: flattens RGBA -> RGB once (lazily) and reuses it
        # across this size's JPEG outputs.  Same primitive backfill_avatar_set()
        # uses, so the two paths cannot drift in how alpha/JPEG is handled.
        rendered = _ResizedAvatar(resized)

        for fmt in formats:
            ext = fmt.lower()
            pillow_fmt = FORMAT_MAP[ext][0]
            out_path = size_dir / f"{filename_base}.{ext}"
            log.debug("Saving %s as %s.", key, ext.upper())

            _save_image(rendered.for_format(pillow_fmt), out_path, pillow_fmt)

            file_size = out_path.stat().st_size
            total_bytes += file_size
            results[key][ext] = f"{AVATAR_BASE_URL}/{key}/{filename_base}.{ext}"
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
    # JPEG has no alpha channel, so RGBA must be flattened first.  WebP and AVIF
    # support alpha natively so the original image is passed through untouched.
    if pillow_fmt == "JPEG":
        resized = _flatten_rgba_to_rgb(resized)

    # PNG is lossless (quality=None); JPEG/WebP/AVIF use a configurable quality level
    if pillow_fmt == "JPEG":
        quality = img_jpeg_quality
    elif pillow_fmt == "WEBP":
        quality = img_webp_quality
    elif pillow_fmt == "AVIF":
        quality = img_avif_quality
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

    # Quality reduction loop (JPEG / WebP / AVIF only; PNG is lossless)
    if quality is None:
        raise ValueError(
            f"PNG image at {target_size}x{target_size} is {len(data)} bytes, "
            f"exceeding the {max_file_size_kb} KB limit. PNG is lossless and quality "
            f"cannot be reduced. Use JPEG, WebP, or AVIF, or increase max_file_size."
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

    Iterates every configured size x format combination and deletes the
    corresponding file.  Used both for rollback on upload failure and for
    retention cleanup.

    Returns (deleted, failed): files successfully removed and files that raised
    an OSError.  Files that simply do not exist are silently skipped (not
    counted as failures).
    """
    log.info("Cleaning up avatar files for %s.", filename_base)
    sizes = img_sizes
    formats = img_formats
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


# Backfill (regenerate missing sizes/formats)
#
# Rank lookup derived from BACKFILL_SOURCE_PREFERENCE so image_formats.py stays
# the single source of truth for format metadata (adding a format only requires
# touching that file).  Lower rank = preferred regeneration source; extensions
# not listed there sort last but still work.
_SOURCE_EXT_RANK: dict[str, int] = {
    ext: rank for rank, ext in enumerate(BACKFILL_SOURCE_PREFERENCE)
}

# Configured formats ordered by regeneration-source preference, computed once.
# The candidate list _load_largest_source_image() walks is identical for every
# avatar set (img_formats is fixed at import time), so there is no need to
# re-sort it on each call.  img_formats entries are already canonical on-disk
# extensions; extensions absent from BACKFILL_SOURCE_PREFERENCE sort last.
_SOURCE_EXTS_BY_PREFERENCE: list[str] = sorted(
    img_formats,
    key=lambda e: _SOURCE_EXT_RANK.get(e, len(BACKFILL_SOURCE_PREFERENCE)),
)


def _load_largest_source_image(filename_base: str) -> tuple[Image.Image, int] | None:
    """
    Load the highest-resolution on-disk image for one avatar set, to use as the
    source when regenerating missing sizes/formats.

    Searches the configured sizes from largest to smallest and, within each
    size, prefers lossless / higher-fidelity formats (BACKFILL_SOURCE_PREFERENCE
    in image_formats.py) so a lossless PNG is chosen over a lossy JPEG at the
    same resolution.

    Returns ``(image, source_size)`` for the first file that decodes
    successfully, or ``None`` when no file for this set can be read.
    """
    # Candidate extensions are the configured formats ordered by fidelity
    # (precomputed in _SOURCE_EXTS_BY_PREFERENCE).  img_formats entries are
    # already canonical on-disk extensions (config.py resolves them through
    # FORMAT_MAP), matching process_image()'s file naming.
    for size in sorted(img_sizes, reverse=True):
        size_dir = AVATAR_ROOT / f"{size}x{size}"
        for ext in _SOURCE_EXTS_BY_PREFERENCE:
            candidate = size_dir / f"{filename_base}.{ext}"
            if not candidate.is_file():
                continue
            try:
                # Open inside a context manager and force-decode with load() so
                # the returned copy is detached from the (now closed) file.
                with Image.open(candidate) as img:
                    img.load()
                    return img.copy(), size
            except Exception as exc:
                log.warning(
                    "Could not decode %s as a backfill source: %s", candidate, exc
                )
    return None


def backfill_avatar_set(filename_base: str) -> tuple[int, int, int]:
    """
    Ensure one avatar set has a file for every configured size x format,
    regenerating only the ones that are missing.

    When ``images.sizes`` or ``images.formats`` gains a new entry, avatars that
    were uploaded before the change have no file for the new size/format.  This
    regenerates the missing files from the largest image already on disk for the
    set, so existing avatars become available in the new size/format without
    requiring users to re-upload.  Existing files are never overwritten.

    If the only available source is smaller than a missing size, the image is
    upscaled (logged as a warning): the original full-resolution upload is not
    retained, so the largest stored size is the best source available.

    Returns ``(generated, failed, skipped)``:
      - generated: files successfully created (or, in dry-run, that would be).
      - failed: files that could not be created because a resize/encode/write
        raised - a genuine error worth counting against the run.
      - skipped: files left missing because the set has no readable source
        image to regenerate from.  This is an anomaly (metadata survives but
        the pixels are gone), reported but deliberately not counted as a
        failure, since no amount of retrying can resolve it.
    Returns ``(0, 0, 0)`` when the set is already complete.
    Respects dry_run mode (logs intent, writes nothing).
    """
    # Group the missing outputs by size so each size is resized only once and
    # the result is reused across that size's formats.  Each entry is the Pillow
    # save format and the target path.  img_formats entries are already the
    # canonical on-disk extensions (config.py resolves them through FORMAT_MAP),
    # so the naming matches process_image() and the cleanup job's orphan check.
    missing_by_size: dict[int, list[tuple[str, Path]]] = defaultdict(list)
    for size in img_sizes:
        size_dir = AVATAR_ROOT / f"{size}x{size}"
        for ext in img_formats:
            out_path = size_dir / f"{filename_base}.{ext}"
            if not out_path.exists():
                missing_by_size[size].append((FORMAT_MAP[ext][0], out_path))

    if not missing_by_size:
        # Set already has every configured size x format on disk - nothing to do.
        log.debug("Backfill check: %s is complete, no missing files.", filename_base)
        return 0, 0, 0

    total_missing = sum(len(combos) for combos in missing_by_size.values())

    # A configured size/format is missing for this set: it needs backfilling.
    log.debug(
        "Backfill check: %s is missing %d file(s), regenerating from largest source.",
        filename_base,
        total_missing,
    )

    # Load the best available source once and reuse it for every missing size.
    source = _load_largest_source_image(filename_base)
    if source is None:
        # Metadata survives but no decodable image remains: nothing to
        # regenerate from.  Report as "skipped" (not "failed") - retrying can
        # never fix it, so it must not inflate the run's failure count.
        log.warning(
            "Cannot backfill %d missing file(s) for %s - no readable source image.",
            total_missing,
            filename_base,
        )
        return 0, 0, total_missing

    source_image, source_size = source
    # The source is one of our own outputs: process_image() already ran the full
    # normalize_image() treatment (EXIF orientation, metadata strip) before the
    # file was written, so repeating its unconditional pixel-buffer rebuild here
    # would be pure waste.  Only the mode guard is kept - a no-op for our own
    # RGB/RGBA outputs, it protects against legacy or hand-placed files.  It is
    # wrapped so one odd image skips this set instead of aborting the whole
    # backfill phase of the cleanup run.
    if source_image.mode not in ("RGB", "RGBA"):
        try:
            source_image = source_image.convert("RGBA")
        except Exception:
            log.exception(
                "Could not convert backfill source for %s (mode %r) - skipping set.",
                filename_base,
                source_image.mode,
            )
            return 0, 0, total_missing

    generated = 0
    failed = 0
    for size, combos in missing_by_size.items():
        if size > source_size:
            log.warning(
                "Backfilling %dx%d for %s by upscaling from %dx%d - the original "
                "upload is not retained, so output quality is limited.",
                size,
                size,
                filename_base,
                source_size,
                source_size,
            )
        try:
            resized = source_image.resize((size, size), Image.LANCZOS)
        except Exception:
            log.exception(
                "Failed to resize %s to %dx%d during backfill.",
                filename_base,
                size,
                size,
            )
            failed += len(combos)
            continue

        # Same shared renderer process_image() uses: flatten RGBA -> RGB once
        # per size, reused across this size's JPEG outputs.
        rendered = _ResizedAvatar(resized)
        for pillow_fmt, out_path in combos:
            try:
                rel = f"{size}x{size}/{out_path.name}"
                if dry_run:
                    log.info("[DRY-RUN] Would backfill missing avatar file %s.", rel)
                else:
                    # Create the size directory in case a newly-added size has no
                    # directory yet, so the save does not raise (BUG-01).
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    _save_image(rendered.for_format(pillow_fmt), out_path, pillow_fmt)
                    log.info("Backfilled missing avatar file %s.", rel)
                generated += 1
            except Exception:
                log.exception("Failed to backfill %s.", out_path)
                failed += 1

    # skipped is 0 here: a readable source existed, so every missing file was
    # either generated or counted as a hard failure above.
    return generated, failed, 0


def _read_meta(path: Path) -> dict | None:
    """Read and parse a single metadata JSON file. Returns the dict, or None on any read or parse error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_all_avatar_metadata() -> list[dict]:
    """
    Read and return every .meta.json file from AVATAR_ROOT.

    Used by the cleanup job to compare on-disk avatar ownership
    against the set of active Authentik users.
    """
    entries = []
    for meta_path in METADATA_ROOT.glob("*.meta.json"):
        meta = _read_meta(meta_path)
        if meta is not None:
            entries.append(meta)
        else:
            log.warning("Skipping unreadable metadata file %s.", meta_path)
    log.debug("Loaded %d metadata file(s) from %s.", len(entries), METADATA_ROOT)
    return entries


def load_metadata_file(filename: str) -> dict | None:
    """
    Read and parse a single metadata JSON file from METADATA_ROOT.

    Returns the parsed dict, or None if the file is missing or unreadable.
    Used by the metadata serve endpoint for ownership checks before serving.

    Defense-in-depth: the resolved path is verified to stay within METADATA_ROOT
    even though Flask's ``<filename>`` URL converter already rejects slashes.
    """
    meta_path = (METADATA_ROOT / filename).resolve()
    # Reject any path that escapes the metadata root (e.g. via ".." components)
    if not meta_path.is_relative_to(METADATA_ROOT.resolve()):
        log.warning("Metadata path traversal blocked: %s", filename)
        return None
    meta = _read_meta(meta_path)
    if meta is None:
        log.debug("Could not read metadata file %s.", filename)
    return meta
