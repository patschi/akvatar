"""
upload.py - Avatar upload processing pipeline.

Handles the full lifecycle of an avatar upload:
  1. Validate the uploaded file (extension, magic bytes, Pillow decode, dimensions)
  2. Stream processing progress to the client as Server-Sent Events (SSE):
     - Normalize the image (EXIF orientation, metadata strip, color mode)
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

import json
import logging
from datetime import UTC, datetime

from PIL import Image

from src.authentik import revert_avatar_url, update_avatar_url
from src.config import ak_avatar_ext, ak_avatar_size, dry_run, img_formats, img_sizes
from src.i18n import t
from src.image_formats import FORMAT_MAP
from src.imaging import (
    AVATAR_BASE_URL,
    METADATA_ROOT,
    cleanup_avatar_files,
    normalize_image,
    prepare_ldap_image,
    process_image,
)
from src.ldap_client import get_photos_config as ldap_photos_config
from src.ldap_client import is_enabled as ldap_is_enabled
from src.ldap_client import update_photos as update_ldap_photos

log = logging.getLogger("upload")

# Module-level config (immutable after startup)
_ldap_enabled = ldap_is_enabled()
_ldap_photos = ldap_photos_config()


# SSE helper


# Canonical avatar URL helpers
#
# The "canonical" URL is the JPG at the Authentik avatar size - the single URL
# pushed to Authentik's user profile.  Two callers need it:
#   - api_upload (routes.py): pre-computes it so the URL can be stored in the
#     session cookie before the SSE stream starts.
#   - _step_sync_authentik: looks it up from the processed output map to push
#     to Authentik.
# Both go through the same size/format constants so they cannot diverge.

_CANONICAL_SIZE_KEY = f"{ak_avatar_size}x{ak_avatar_size}"
# Canonical file extension comes from config validation (config.py resolves
# "jpeg"/"jpg" to the canonical "jpg" extension via FORMAT_MAP).
_CANONICAL_FORMAT = ak_avatar_ext


def build_canonical_url(filename_base: str) -> str:
    """Build the canonical avatar URL for a given filename base."""
    return (
        f"{AVATAR_BASE_URL}/{_CANONICAL_SIZE_KEY}/{filename_base}.{_CANONICAL_FORMAT}"
    )


def _sse(data: dict) -> str:
    """Format a dict as a single Server-Sent Event frame."""
    return f"data: {json.dumps(data)}\n\n"


# Pipeline steps
#
# Steps that need to both yield SSE frames and return data use Python's
# generator-return: yield SSE strings, return the result.  The caller
# collects via `result = yield from step(...)`.


def _step_prepare_image(image: Image.Image):
    """Apply EXIF orientation, strip all metadata, and normalize the color mode."""
    normalized = normalize_image(image)
    yield _sse({"step": t("step.prepare"), "status": "success"})
    return normalized


def _step_process_image(image: Image.Image, filename_base: str):
    """Resize & save the image in all configured sizes/formats."""
    urls, total_bytes = process_image(image, filename_base)
    if not urls:
        raise RuntimeError(
            "Image processing produced no output - check images.sizes/formats config."
        )

    if total_bytes >= 1_048_576:
        size_label = f"{total_bytes / 1_048_576:.1f} MB"
    else:
        size_label = f"{total_bytes / 1024:.0f} KB"

    yield _sse(
        {
            "step": t("step.processed"),
            "status": "success",
            "detail": t(
                "step.processed_detail",
                sizes=len(img_sizes),
                formats=len(img_formats),
                total=size_label,
            ),
        }
    )
    return urls, total_bytes


def _resolve_canonical_url(urls: dict) -> str:
    """
    Look up the canonical avatar URL (used by Authentik) from the generated
    URL map.  Raises RuntimeError if the expected size/format is missing.
    """
    canonical = urls.get(_CANONICAL_SIZE_KEY, {}).get(_CANONICAL_FORMAT)
    if not canonical:
        raise RuntimeError(
            f"Canonical avatar URL not found: size={_CANONICAL_SIZE_KEY}, "
            f"format={_CANONICAL_FORMAT}. Ensure {ak_avatar_size} is in "
            f'images.sizes and "{_CANONICAL_FORMAT}" is in images.formats.'
        )
    log.debug("Canonical Authentik avatar URL: %s", canonical)
    return canonical


def _step_sync_authentik(user_pk: int, canonical_url: str):
    """
    Push the avatar URL to Authentik via API.

    Yields one SSE frame.  Returns ``(ak_attrs, old_avatar_url, failed)``.
    *old_avatar_url* is the previous value so it can be restored on rollback.
    On failure the pipeline continues so LDAP can be skipped gracefully.
    """
    try:
        ak_attrs, old_url = update_avatar_url(user_pk, canonical_url)
        if not isinstance(ak_attrs, dict):
            raise TypeError(
                f"Authentik API returned {type(ak_attrs).__name__} instead of dict."
            )
        yield _sse(
            {
                "step": t("step.profile_synced"),
                "status": "dry-run" if dry_run else "success",
            }
        )
        return ak_attrs, old_url, False
    except Exception:
        log.exception("Failed to update Authentik avatar for pk=%s.", user_pk)
        yield _sse({"step": t("step.profile_synced"), "status": "failed"})
        return {}, None, True


def _build_ldap_updates(
    image: Image.Image, urls: dict, filename_base: str
) -> list[dict]:
    """
    Build LDAP attribute updates from the ``ldap.photos`` config.

    For ``binary`` entries the image is encoded on-the-fly (or reused from disk).
    For ``url`` entries the pre-generated public URL is looked up.
    """
    updates = []
    for photo_cfg in _ldap_photos:
        attr = photo_cfg["attribute"]
        ptype = photo_cfg["type"]
        size = photo_cfg["image_size"]
        img_type = photo_cfg["image_type"]

        if ptype == "binary":
            img_bytes = prepare_ldap_image(
                image,
                filename_base,
                size,
                img_type,
                photo_cfg.get("max_file_size", 0),
            )
            updates.append({"attribute": attr, "value": img_bytes})
            log.info(
                "Prepared LDAP %s: %dx%d %s, %d bytes.",
                attr,
                size,
                size,
                img_type.upper(),
                len(img_bytes),
            )

        elif ptype == "url":
            size_key = f"{size}x{size}"
            ext = FORMAT_MAP[img_type][1]
            url = urls.get(size_key, {}).get(ext)
            if not url:
                raise ValueError(
                    f"No pre-generated URL for LDAP {attr}: "
                    f"size={size_key}, ext={ext}. Check images.sizes/formats config."
                )
            updates.append({"attribute": attr, "value": url})
            log.info("Prepared LDAP %s: URL → %s.", attr, url)

        else:
            log.warning(
                "Unknown LDAP photo type %r for attribute %s - skipping.", ptype, attr
            )

    return updates


def _step_sync_ldap(
    image: Image.Image, urls: dict, filename_base: str, ak_attrs: dict, user_pk: int
):
    """
    Update LDAP photo attributes if applicable.

    Yields SSE frames.  Returns True on failure, False on success/skip.
    Skips silently when LDAP is disabled or the user has no ``ldap_uniq``.
    """
    if not (_ldap_enabled and _ldap_photos):
        return False

    ldap_uniq = ak_attrs.get("ldap_uniq")

    # Users without ldap_uniq are Authentik-only (not synced from LDAP)
    if not ldap_uniq:
        log.info("User pk=%s has no ldap_uniq - skipping LDAP updates.", user_pk)
        yield _sse({"step": t("step.ldap_updated"), "status": "skipped"})
        return False

    log.debug(
        "User has ldap_uniq=%r - preparing %d LDAP photo update(s).",
        ldap_uniq,
        len(_ldap_photos),
    )
    try:
        ldap_updates = _build_ldap_updates(image, urls, filename_base)
        update_ldap_photos(ldap_uniq, ldap_updates)
        yield _sse(
            {
                "step": t("step.ldap_updated"),
                "status": "dry-run" if dry_run else "success",
            }
        )
        return False
    except Exception:
        log.exception("Failed to update LDAP for ldap_uniq=%s.", ldap_uniq)
        yield _sse({"step": t("step.ldap_updated"), "status": "failed"})
        return True


def _save_metadata(filename_base: str, user_pk: int, total_bytes: int) -> None:
    """
    Persist upload metadata as JSON.  Uses the Authentik PK (immutable, no PII)
    as the owner identifier for cleanup/retention matching.
    """
    metadata = {
        "filename": filename_base,
        "user_pk": user_pk,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "sizes": img_sizes,
        "formats": img_formats,
        "total_bytes": total_bytes,
    }
    meta_path = METADATA_ROOT / f"{filename_base}.meta.json"
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    log.debug("Metadata saved to %s.", meta_path)


# Main SSE generator - orchestrates the full pipeline


def generate_sse(user: dict, image: Image.Image, filename_base: str):
    """
    Generator that drives the upload pipeline and yields SSE frames.

    ``user`` is the session user dict (must contain ``pk`` and ``username``).
    ``image`` is the already-validated (but not yet normalized) PIL Image.
    ``filename_base`` is the secure filename pre-generated by the caller so
    that the canonical URL can be stored in the session cookie before the SSE
    stream begins (Flask commits cookie headers before the generator runs).
    """
    username = user["username"]
    user_pk = user["pk"]

    try:
        yield _sse(
            {
                "step": t("step.validated"),
                "status": "success",
                "detail": t("step.validated_detail"),
            }
        )

        # Normalize image (EXIF orientation, metadata strip, color mode)
        image = yield from _step_prepare_image(image)

        # Resize and save all configured sizes and formats
        urls, total_bytes = yield from _step_process_image(image, filename_base)

        # Resolve the canonical avatar URL (the single URL pushed to Authentik)
        canonical_url = _resolve_canonical_url(urls)

        # Push the avatar URL to Authentik
        ak_attrs, old_avatar_url, ak_failed = yield from _step_sync_authentik(
            user_pk, canonical_url
        )

        # Update LDAP photo attributes (if applicable)
        ldap_failed = yield from _step_sync_ldap(
            image, urls, filename_base, ak_attrs, user_pk
        )

        # Rollback on any backend failure
        if ak_failed or ldap_failed:
            log.warning("Backend update failed - rolling back for %s.", filename_base)
            # Revert Authentik if it was already updated successfully
            if not ak_failed and not dry_run:
                try:
                    revert_avatar_url(user_pk, old_avatar_url)
                    log.debug("Authentik avatar reverted for pk=%s.", user_pk)
                except Exception:
                    log.exception(
                        "Failed to revert Authentik avatar for pk=%s.", user_pk
                    )
            cleanup_avatar_files(filename_base)
            yield _sse({"step": t("step.rollback"), "status": "success"})
            yield _sse({"done": True, "error": t("result.error")})
            return

        # Step 7: Persist metadata
        _save_metadata(filename_base, user_pk, total_bytes)

        # Session update is handled by the caller via /api/upload/commit:
        # the client calls that endpoint after receiving this done event, which
        # runs in a normal request/response cycle where the cookie is properly
        # committed.

        log.info("Upload pipeline complete for user %r (pk=%s).", username, user_pk)
        yield _sse({"done": True, "avatar_url": canonical_url})

    except Exception:
        log.exception("Upload processing failed for user %r.", username)
        if filename_base:
            cleanup_avatar_files(filename_base)
        # Show a vague user-friendly message - never expose internal errors to the client
        yield _sse(
            {
                "step": t("step.processing_failed"),
                "status": "failed",
                "detail": t("step.save_failed"),
            }
        )
        yield _sse({"done": True, "error": "contact_admin"})
