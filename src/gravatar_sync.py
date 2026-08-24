"""
gravatar_sync.py - One-time Gravatar backfill / sync engine.

Walks Authentik users, fetches each one's Gravatar image, and imports it
through the exact same processing pipeline as a manual upload (server-side
resize to every configured size/format, Authentik attribute write, optional
LDAP write, metadata, and optional webhooks).  No cropping is applied -
Gravatar serves square images.

Behavior per user:
  - A user whose current avatar was set through the web UI (metadata
    ``source`` is not ``"gravatar_sync"``) is never overwritten.
  - A user whose current avatar was applied by this job is re-checked: when
    their Gravatar image changed (the sha256 of a small fixed-size probe image
    differs from the stored hash) the new image is re-imported; otherwise it is
    left as-is.
  - A user with no current avatar and no prior avatar metadata is filled from
    Gravatar (a first-time import).
  - A user with no current avatar but with prior metadata on disk removed their
    avatar and is left alone (not re-filled).

Right before publishing, the user's live Authentik record is re-read and
compared against the snapshot the decision was based on, so an avatar set
through the web UI while a long bulk run is in progress is never overwritten.

This module holds only the engine.  The manual entry point is
``run_sync_gravatar.py`` at the project root.  A scheduled/background variant is
planned separately; the per-user decision logic lives here so both can share it.
"""

import hashlib
import logging
import time

from src.authentik import get_user, list_users, revert_avatar_url, update_avatar_url
from src.avatar_pipeline import (
    CLEANUP_LOCKFILE,
    GRAVATAR_SYNC_LOCKFILE,
    build_webhook_context,
    exclusive_process_lock,
    resolve_canonical_url,
    save_avatar_metadata,
    sync_ldap_photos,
)
from src.config import (
    ak_avatar_id_attribute,
    dry_run,
    img_sizes,
    skip_backend_writes,
)
from src.image_formats import ALLOWED_EXTENSIONS, MIME_TO_EXT
from src.image_import import (
    FetchFailed,
    GravatarNotFound,
    ImageTooLarge,
    UnsupportedContentType,
    fetch_gravatar_image,
)
from src.image_validation import ValidationError, validate_image_bytes
from src.imaging import (
    cleanup_avatar_files,
    generate_filename,
    get_all_avatar_metadata,
    normalize_image,
    process_image,
)
from src.webhooks import fire_webhooks, wait_for_pending_deliveries

log = logging.getLogger("grav_sync")

# Square pixel size requested from Gravatar for the actual import.  Use the
# largest configured avatar size so the fetched source is at least as large as
# every generated output (no upscaling during processing), capped at Gravatar's
# 2048 maximum.
FETCH_SIZE = min(2048, max(img_sizes))

# Square pixel size of the small "probe" image used for change detection.  The
# stored ``gravatar_hash`` is the sha256 of this probe, NOT of the full-size
# image, for two reasons: it is independent of ``images.sizes`` (so changing
# the configured sizes does not invalidate every stored hash and trigger a
# mass re-import), and re-checking thousands of unchanged users per run only
# transfers a few KB each instead of a full-size image.
HASH_PROBE_SIZE = 80

# The metadata source marker written for avatars this job creates.  Any other
# value (or a missing field on a pre-existing avatar) marks a user avatar the
# job must never overwrite.
_SYNC_SOURCE = "gravatar_sync"

# Upper bound (seconds) to wait for in-flight webhook deliveries at the end of
# a run before returning to the (short-lived) CLI process.
_WEBHOOK_DRAIN_TIMEOUT_S = 30.0


class _GravatarError(Exception):
    """A non-404 failure while fetching a Gravatar image (network, size, type)."""


class _UnsupportedFormat(Exception):
    """Gravatar served an image format the processing pipeline does not accept."""


class _AvatarChangedConcurrently(Exception):
    """The user's live avatar differs from the snapshot - someone else set it."""


def _fetch_gravatar(email: str, size: int) -> tuple[bytes, str] | None:
    """
    Fetch the Gravatar image for *email* at *size* pixels.

    Returns ``(image_bytes, filename)`` when an avatar exists, ``None`` when
    Gravatar has no avatar for this email (HTTP 404).  Raises
    :class:`_UnsupportedFormat` when the served format passes the proxy MIME
    allowlist but is not an accepted upload format (e.g. GIF), and
    :class:`_GravatarError` on any other fetch failure.
    """
    try:
        data, content_type, filename = fetch_gravatar_image(email, size=size)
    except GravatarNotFound:
        return None
    except (FetchFailed, ImageTooLarge, UnsupportedContentType) as exc:
        raise _GravatarError(str(exc)) from exc

    # The proxy allowlist (used by the in-browser import, where the browser
    # crops/re-encodes) is wider than the upload allowlist that the server-side
    # pipeline enforces.  Classify the mismatch here, before any processing.
    ext = MIME_TO_EXT.get(content_type, None)
    if ext not in ALLOWED_EXTENSIONS:
        raise _UnsupportedFormat(content_type)
    return data, filename


def _probe_hash(email: str) -> str | None:
    """
    Fetch the small probe image and return its sha256, or ``None`` when the
    user has no Gravatar.  Propagates the same exceptions as
    :func:`_fetch_gravatar`.
    """
    result = _fetch_gravatar(email, HASH_PROBE_SIZE)
    if result is None:
        return None
    data, _filename = result
    return hashlib.sha256(data).hexdigest()


def _live_avatar_id(pk: int) -> str | None:
    """Re-read the user's current avatar_id attribute from Authentik."""
    live = get_user(pk)
    attrs = live.get("attributes", None)
    avatar_id = (
        attrs.get(ak_avatar_id_attribute, None) if isinstance(attrs, dict) else None
    )
    if not isinstance(avatar_id, str) or not avatar_id:
        return None
    return avatar_id


def _publish(
    user: dict,
    image_bytes: bytes,
    validate_name: str,
    gravatar_hash: str,
    expected_avatar_id: str | None,
    action: str,
    fire_webhooks_enabled: bool,
) -> None:
    """
    Validate the fetched Gravatar bytes and run them through the full avatar
    pipeline: process -> Authentik -> LDAP -> metadata -> optional webhook.

    ``expected_avatar_id`` is the avatar the decision was based on (``None``
    for a first-time import).  The user's live record is re-read before any
    write; if it no longer matches, :class:`_AvatarChangedConcurrently` is
    raised and nothing is touched.

    ``action`` is ``"import"`` or ``"update"`` (used only in log messages).
    Raises on any hard failure after rolling back partial backend writes and
    deleting the generated files, so the caller counts it as a failure.
    In full ``dry_run`` mode nothing is written; the intent is logged instead.
    Under ``dry_run_backend`` the image files are generated (like a web
    upload) but no metadata is written, so the preview leaves no ownership
    record behind: a later real run still performs the first-time import, and
    the orphaned preview files are removed by the next cleanup.
    """
    pk = user["pk"]
    username = user["username"]

    # Validate + decode with the exact same checks as a web upload.
    image = validate_image_bytes(image_bytes, validate_name)

    # Full dry-run: log the intent and write nothing (mirrors the cleanup job).
    if dry_run:
        log.info(
            "[DRY-RUN] Would %s Gravatar avatar for user %r (pk=%s).",
            action,
            username,
            pk,
        )
        return

    # Guard against a stale snapshot: the user list was fetched at run start,
    # and a user may have set their own avatar through the web UI since then.
    live_avatar_id = _live_avatar_id(pk)
    if live_avatar_id != expected_avatar_id:
        raise _AvatarChangedConcurrently(
            f"avatar_id is now {live_avatar_id!r} (expected {expected_avatar_id!r})"
        )

    filename_base = generate_filename()
    normalized = normalize_image(image)
    urls, total_bytes = process_image(normalized, filename_base)
    if not urls:
        cleanup_avatar_files(filename_base)
        raise RuntimeError(
            "Image processing produced no output - check images.sizes/formats config."
        )
    canonical_url = resolve_canonical_url(urls)

    # Track whether a real Authentik PATCH was sent so rollback only reverts a
    # change that actually happened (skip_backend_writes suppresses the PATCH).
    ak_patched = False
    old_url: str | None = None
    old_id: str | None = None
    try:
        ak_attrs, old_url, old_id = update_avatar_url(pk, canonical_url, filename_base)
        ak_patched = not skip_backend_writes

        # LDAP photo attributes (shared gate: enabled + photos + ldap_uniq;
        # update_photos itself is a no-op under skip_backend_writes).
        sync_ldap_photos(normalized, urls, filename_base, ak_attrs, pk)
    except Exception:
        log.exception(
            "Backend publish failed for user %r (pk=%s) - rolling back.", username, pk
        )
        if ak_patched:
            try:
                revert_avatar_url(pk, old_url, old_id)
            except Exception:
                log.exception("Failed to revert Authentik avatar for pk=%s.", pk)
        cleanup_avatar_files(filename_base)
        raise

    if skip_backend_writes:
        # No Authentik PATCH happened, so no ownership record may be written:
        # metadata without a matching Authentik avatar_id would make the next
        # real run believe the user removed their avatar and skip them forever.
        log.info(
            "[DRY-RUN] Would %s Gravatar avatar for user %r (pk=%s) - files "
            "generated as %s, metadata not written.",
            action,
            username,
            pk,
            filename_base,
        )
        return

    # Persist metadata: source marks this avatar as job-owned and the hash lets
    # a later run detect when the user's Gravatar image has changed.
    save_avatar_metadata(
        filename_base,
        pk,
        total_bytes,
        source=_SYNC_SOURCE,
        gravatar_hash=gravatar_hash,
    )

    # Webhooks are opt-in for the sync (off by default) so a bulk backfill does
    # not fan out a notification per user.  fire_webhooks itself is a no-op
    # when webhooks are disabled in config.
    if fire_webhooks_enabled:
        fire_webhooks(
            build_webhook_context(user, canonical_url, filename_base, total_bytes)
        )

    log.info(
        "Gravatar %s complete for user %r (pk=%s): %s",
        action,
        username,
        pk,
        canonical_url,
    )


def _import_from_gravatar(
    user: dict,
    probe_hash: str,
    expected_avatar_id: str | None,
    action: str,
    fire_webhooks_enabled: bool,
    counts: dict[str, int],
) -> None:
    """
    Download the full-size Gravatar image and publish it, updating ``counts``
    with the outcome (``imported``/``updated``, ``skipped_custom``,
    ``unsupported``, ``no_gravatar`` or ``failed``).
    """
    pk = user["pk"]
    username = user["username"]

    try:
        result = _fetch_gravatar(user["email"], FETCH_SIZE)
    except _UnsupportedFormat as exc:
        log.info(
            "Gravatar for user %r (pk=%s) is %s - unsupported format, skipping.",
            username,
            pk,
            exc,
        )
        counts["unsupported"] += 1
        return
    except _GravatarError as exc:
        log.warning("Gravatar fetch failed for user %r (pk=%s): %s", username, pk, exc)
        counts["failed"] += 1
        return

    if result is None:
        # Existed a moment ago for the probe; treat like any other 404.
        log.debug("No Gravatar for user %r (pk=%s).", username, pk)
        counts["no_gravatar"] += 1
        return

    data, filename = result
    try:
        _publish(
            user,
            data,
            filename,
            probe_hash,
            expected_avatar_id,
            action,
            fire_webhooks_enabled,
        )
    except _AvatarChangedConcurrently as exc:
        log.info(
            "User %r (pk=%s) changed their avatar during this run (%s) - "
            "leaving untouched.",
            username,
            pk,
            exc,
        )
        counts["skipped_custom"] += 1
        return
    except ValidationError as exc:
        # Bytes passed the MIME allowlist but failed the upload validation
        # (corrupt data, dimension limits, ...): not a bug, so no traceback.
        log.warning(
            "Gravatar image for user %r (pk=%s) rejected by validation: %s",
            username,
            pk,
            exc,
        )
        counts["failed"] += 1
        return
    except Exception:
        log.exception(
            "Failed to %s Gravatar avatar for user %r (pk=%s).", action, username, pk
        )
        counts["failed"] += 1
        return
    counts["imported" if action == "import" else "updated"] += 1


def _process_user(
    user: dict,
    meta_by_filename: dict[str, dict],
    pks_with_meta: set[int],
    fire_webhooks_enabled: bool,
    counts: dict[str, int],
) -> bool:
    """
    Apply the per-user decision logic for one Authentik user.

    Returns ``True`` when at least one Gravatar request was made for this user
    (so the caller can throttle only after real network activity).
    """
    pk = user["pk"]
    username = user["username"]
    email = user["email"]

    # Announce the user before any decision or Gravatar request is made, so a
    # run that hangs, crashes, or is interrupted can be traced back to the exact
    # user it was working on.
    log.info(
        "Checking user %r (pk=%s, email=%s)...",
        username,
        pk,
        email if email else "-",
    )

    if not email:
        log.debug("User %r (pk=%s) has no email - skipping.", username, pk)
        counts["no_email"] += 1
        return False

    avatar_id = user["attributes"].get(ak_avatar_id_attribute, None)
    # Treat an empty/non-string attribute as "no avatar".
    if not isinstance(avatar_id, str) or not avatar_id:
        avatar_id = None

    if avatar_id:
        # A current avatar exists - only touch it if this job created it.
        meta = meta_by_filename.get(avatar_id, None)
        source = meta.get("source", "web") if meta else "web"
        if source != _SYNC_SOURCE:
            log.debug(
                "User %r (pk=%s) has a user-set avatar - leaving untouched.",
                username,
                pk,
            )
            counts["skipped_custom"] += 1
            return False
        action = "update"
    elif pk in pks_with_meta:
        # No current avatar, but the user previously had one (any source) that
        # is now gone - they removed it, so it is not auto-refilled.
        log.debug(
            "User %r (pk=%s) previously had an avatar that was removed - not re-adding.",
            username,
            pk,
        )
        counts["skipped_removed"] += 1
        return False
    else:
        meta = None
        action = "import"

    # Cheap probe fetch: detects "no Gravatar", unsupported formats, and (for
    # an existing sync-owned avatar) whether the image changed at all - before
    # the full-size download.
    try:
        probe_hash = _probe_hash(email)
    except _UnsupportedFormat as exc:
        log.info(
            "Gravatar for user %r (pk=%s) is %s - unsupported format, skipping.",
            username,
            pk,
            exc,
        )
        counts["unsupported"] += 1
        return True
    except _GravatarError as exc:
        log.warning("Gravatar fetch failed for user %r (pk=%s): %s", username, pk, exc)
        counts["failed"] += 1
        return True

    if probe_hash is None:
        if action == "update":
            # Gravatar was deleted; keep the existing avatar (do not remove it).
            log.info(
                "Gravatar for user %r (pk=%s) no longer exists - keeping current avatar.",
                username,
                pk,
            )
        else:
            log.debug("No Gravatar for user %r (pk=%s).", username, pk)
        counts["no_gravatar"] += 1
        return True

    if action == "update" and meta and probe_hash == meta.get("gravatar_hash", None):
        log.debug("Gravatar unchanged for user %r (pk=%s).", username, pk)
        counts["unchanged"] += 1
        return True

    _import_from_gravatar(
        user, probe_hash, avatar_id, action, fire_webhooks_enabled, counts
    )
    return True


def _log_summary(counts: dict[str, int], total: int) -> None:
    """Log a single human-readable summary line for the run."""
    log.info(
        "Gravatar sync complete (%d user(s)): %d imported, %d updated, "
        "%d unchanged, %d skipped (user avatar), %d skipped (removed), "
        "%d without Gravatar, %d unsupported format, %d without email, %d failed.",
        total,
        counts["imported"],
        counts["updated"],
        counts["unchanged"],
        counts["skipped_custom"],
        counts["skipped_removed"],
        counts["no_gravatar"],
        counts["unsupported"],
        counts["no_email"],
        counts["failed"],
    )


def _run_impl(
    *, include_deactivated: bool, fire_webhooks_enabled: bool, request_delay_ms: int
) -> dict[str, int]:
    """Run the sync over all target users (called while holding the run locks)."""
    scope = "active + deactivated" if include_deactivated else "active only"
    log.info("Starting Gravatar sync (%s, fetch size %dpx).", scope, FETCH_SIZE)
    if dry_run:
        log.warning("DRY-RUN: no images will be written and no backends updated.")
    elif skip_backend_writes:
        log.warning(
            "DRY-RUN (backend): images will be generated but no backends updated "
            "and no metadata written."
        )

    try:
        users = list_users(active_only=not include_deactivated)
    except Exception:
        log.exception("Failed to fetch users from Authentik - aborting.")
        return {}

    # Safety guard: zero users almost always means a broken token or network,
    # not a genuinely empty directory.  Abort rather than doing nothing silently.
    if not users:
        log.warning(
            "Authentik returned zero users - nothing to do (check API token/connectivity)."
        )
        return {}

    # Load every avatar metadata record once: index by filename (ownership +
    # change detection) and collect the PKs that have any avatar on disk
    # (removal detection).
    all_meta = get_all_avatar_metadata()
    meta_by_filename = {m["filename"]: m for m in all_meta if m.get("filename", None)}
    pks_with_meta = {
        m["user_pk"] for m in all_meta if isinstance(m.get("user_pk", None), int)
    }

    counts = {
        "imported": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_custom": 0,
        "skipped_removed": 0,
        "no_gravatar": 0,
        "unsupported": 0,
        "no_email": 0,
        "failed": 0,
    }

    log.info(
        "Processing %d user(s) against %d existing avatar set(s).",
        len(users),
        len(all_meta),
    )

    delay_s = max(0, request_delay_ms) / 1000.0
    last = len(users) - 1
    for i, user in enumerate(users):
        fetched = False
        try:
            fetched = _process_user(
                user, meta_by_filename, pks_with_meta, fire_webhooks_enabled, counts
            )
        except Exception:
            # Defensive catch-all: one user's unexpected error never aborts the run.
            log.exception(
                "Unexpected error processing user pk=%s.", user.get("pk", None)
            )
            counts["failed"] += 1

        # Optional throttle to limit load on Gravatar - only after a user that
        # actually hit Gravatar; skipped users cost no idle time.
        if delay_s and fetched and i < last:
            time.sleep(delay_s)

    _log_summary(counts, len(users))

    # Deliveries run on daemon threads; make sure none are abandoned when the
    # CLI process exits right after this returns.
    if fire_webhooks_enabled:
        wait_for_pending_deliveries(timeout=_WEBHOOK_DRAIN_TIMEOUT_S)

    return counts


def run_gravatar_sync(
    *,
    include_deactivated: bool = False,
    fire_webhooks_enabled: bool = False,
    request_delay_ms: int = 0,
) -> dict[str, int]:
    """
    Run a one-time Gravatar backfill/sync over Authentik users.

    ``include_deactivated`` also processes deactivated users (default: active
    only).  ``fire_webhooks_enabled`` fires configured webhooks per synced
    avatar (default off, to avoid a notification storm on a bulk run).
    ``request_delay_ms`` pauses between users to throttle Gravatar load.

    Two cross-process locks are held for the whole run: the sync's own lock
    (two manual runs cannot process the same users concurrently) and the
    cleanup lock, so a scheduled cleanup in the Flask process cannot delete
    freshly generated image files before their metadata sidecar exists.

    Returns a dict of per-outcome counts.  Returns ``{}`` immediately if another
    run (or a cleanup) holds a lock, if the user list cannot be fetched, or if
    Authentik returns zero users.
    """
    with exclusive_process_lock(GRAVATAR_SYNC_LOCKFILE, "Gravatar sync") as acquired:
        if not acquired:
            return {}
        with exclusive_process_lock(CLEANUP_LOCKFILE, "Cleanup") as cleanup_free:
            if not cleanup_free:
                log.warning("Gravatar sync skipped: a cleanup run is in progress.")
                return {}
            return _run_impl(
                include_deactivated=include_deactivated,
                fire_webhooks_enabled=fire_webhooks_enabled,
                request_delay_ms=request_delay_ms,
            )
