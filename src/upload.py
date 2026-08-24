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

from PIL import Image

from src.authentik import revert_avatar_url, update_avatar_url
from src.avatar_pipeline import (
    CANONICAL_FORMAT,
    CANONICAL_SIZE_KEY,
    LDAP_PHOTOS_ACTIVE,
    build_webhook_context,
    resolve_canonical_url,
    save_avatar_metadata,
    sync_ldap_photos,
)
from src.config import (
    img_formats,
    img_sizes,
    skip_backend_writes,
)
from src.i18n import t
from src.imaging import (
    AVATAR_BASE_URL,
    AVATAR_ROOT,
    cleanup_avatar_files,
    normalize_image,
    process_image,
)
from src.webhooks import fire_webhooks

log = logging.getLogger("upload")


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


def build_canonical_url(filename_base: str) -> str:
    """Build the canonical avatar URL for a given filename base."""
    return f"{AVATAR_BASE_URL}/{CANONICAL_SIZE_KEY}/{filename_base}.{CANONICAL_FORMAT}"


def pending_avatar_file_exists(filename_base: str) -> bool:
    """Return True if the canonical avatar file for *filename_base* is present on disk.

    Used by /api/upload/commit to detect a stale ``_pending_avatar`` entry
    that survived an SSE-driven rollback.  The pending URL is committed to
    the cookie session before the SSE stream starts (because Flask cannot
    mutate the session from inside a streaming generator); if a later
    pipeline step fails, the rollback in ``generate_sse`` deletes the
    on-disk files but leaves the cookie value behind.  A misbehaving or
    malicious client that calls /api/upload/commit anyway would otherwise
    promote a URL pointing at deleted files into the active avatar.
    """
    canonical_path = (
        AVATAR_ROOT / CANONICAL_SIZE_KEY / f"{filename_base}.{CANONICAL_FORMAT}"
    )
    return canonical_path.is_file()


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


def _step_sync_authentik(user_pk: int, canonical_url: str, avatar_id: str):
    """
    Push the avatar URL and avatar ID to Authentik via API.

    Yields one SSE frame.  Returns
    ``(ak_attrs, old_avatar_url, old_avatar_id, failed)``.  The two ``old_*``
    values are the previous attribute values so they can be restored on
    rollback.  On failure the pipeline continues so LDAP can be skipped
    gracefully.
    """
    try:
        ak_attrs, old_url, old_id = update_avatar_url(user_pk, canonical_url, avatar_id)
        if not isinstance(ak_attrs, dict):
            raise TypeError(
                f"Authentik API returned {type(ak_attrs).__name__} instead of dict."
            )
        yield _sse(
            {
                "step": t("step.profile_synced"),
                # Authentik write is suppressed under full dry_run or dry_run_backend.
                "status": "dry-run" if skip_backend_writes else "success",
            }
        )
        return ak_attrs, old_url, old_id, False
    except Exception:
        log.exception("Failed to update Authentik avatar for pk=%s.", user_pk)
        yield _sse({"step": t("step.profile_synced"), "status": "failed"})
        return {}, None, None, True


def _step_sync_ldap(
    image: Image.Image, urls: dict, filename_base: str, ak_attrs: dict, user_pk: int
):
    """
    Update LDAP photo attributes if applicable.

    Yields SSE frames.  Returns True on failure, False on success/skip.
    The enabled / ``ldap_uniq`` gate lives in the shared ``sync_ldap_photos``
    so the Gravatar sync applies exactly the same skip logic.
    """
    if not LDAP_PHOTOS_ACTIVE:
        return False

    try:
        applied = sync_ldap_photos(image, urls, filename_base, ak_attrs, user_pk)
    except Exception:
        log.exception(
            "Failed to update LDAP for ldap_uniq=%s.", ak_attrs.get("ldap_uniq", None)
        )
        yield _sse({"step": t("step.ldap_updated"), "status": "failed"})
        return True

    if not applied:
        # Users without ldap_uniq are Authentik-only (not synced from LDAP)
        yield _sse({"step": t("step.ldap_updated"), "status": "skipped"})
        return False

    yield _sse(
        {
            "step": t("step.ldap_updated"),
            # LDAP write is suppressed under full dry_run or dry_run_backend.
            "status": "dry-run" if skip_backend_writes else "success",
        }
    )
    return False


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
        canonical_url = resolve_canonical_url(urls)

        # Push the avatar URL and avatar ID (filename_base) to Authentik
        (
            ak_attrs,
            old_avatar_url,
            old_avatar_id,
            ak_failed,
        ) = yield from _step_sync_authentik(user_pk, canonical_url, filename_base)

        # Update LDAP photo attributes (if applicable)
        ldap_failed = yield from _step_sync_ldap(
            image, urls, filename_base, ak_attrs, user_pk
        )

        # Rollback on any backend failure
        if ak_failed or ldap_failed:
            log.warning("Backend update failed - rolling back for %s.", filename_base)
            # Revert Authentik only if a real PATCH was actually sent: it
            # succeeded (not ak_failed) and writes were not suppressed (neither
            # full dry_run nor dry_run_backend).  Otherwise there is nothing to undo.
            if not ak_failed and not skip_backend_writes:
                try:
                    revert_avatar_url(user_pk, old_avatar_url, old_avatar_id)
                    log.debug("Authentik avatar reverted for pk=%s.", user_pk)
                except Exception:
                    log.exception(
                        "Failed to revert Authentik avatar for pk=%s.", user_pk
                    )
            cleanup_avatar_files(filename_base)
            yield _sse({"step": t("step.rollback"), "status": "success"})
            yield _sse({"done": True, "error": t("result.error")})
            return

        # Persist metadata (source="web": any avatar set through the web UI, so
        # the Gravatar sync recognizes it as a user avatar and never overwrites it)
        save_avatar_metadata(filename_base, user_pk, total_bytes, source="web")

        # Fire outgoing webhooks (non-blocking) now that the update is a full
        # success.  Delivery runs in a background thread; failures are logged
        # inside fire_webhooks and never affect the user's result.
        fire_webhooks(
            build_webhook_context(user, canonical_url, filename_base, total_bytes)
        )

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
