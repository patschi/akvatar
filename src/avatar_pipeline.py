"""
avatar_pipeline.py - Shared avatar backend-publish helpers.

Holds the pure, non-SSE building blocks used both by the interactive upload
pipeline (``upload.py``) and by the background Gravatar sync
(``gravatar_sync.py``):

  - resolve_canonical_url : pick the single avatar URL pushed to Authentik.
  - build_ldap_updates    : turn the generated image set into LDAP attribute writes.
  - sync_ldap_photos      : apply those LDAP writes (with the enabled/ldap_uniq gate).
  - save_avatar_metadata  : persist the per-avatar ``.meta.json`` sidecar.
  - build_webhook_context : shape the placeholder map handed to fire_webhooks.
  - exclusive_process_lock: cross-process flock guard shared by the background jobs.

Keeping these here (instead of private to upload.py) means the two publish paths
cannot drift in how they name the canonical URL, prepare LDAP values, or shape
the metadata record.
"""

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from src import APP_NAME, APP_VERSION
from src.config import (
    ak_avatar_ext,
    ak_avatar_size,
    img_formats,
    img_sizes,
)
from src.image_formats import FORMAT_MAP
from src.imaging import AVATAR_ROOT, METADATA_ROOT, prepare_ldap_image
from src.ldap_client import get_photos_config
from src.ldap_client import is_enabled as ldap_is_enabled
from src.ldap_client import update_photos as update_ldap_photos

log = logging.getLogger("avatar_pipeline")

# Canonical avatar identifiers.
#
# The "canonical" avatar is the single size/format pushed to Authentik's user
# profile.  Both publish paths resolve it through these constants so they can
# never disagree on which generated file is the canonical one.
CANONICAL_SIZE_KEY = f"{ak_avatar_size}x{ak_avatar_size}"
# Canonical file extension comes from config validation (config.py resolves
# "jpeg"/"jpg" to the canonical "jpg" extension via FORMAT_MAP).
CANONICAL_FORMAT = ak_avatar_ext

# Configured LDAP photo attributes (empty when LDAP is disabled or unconfigured).
# Read once at import time; config is immutable after startup.
_LDAP_PHOTOS = get_photos_config()

# Whether LDAP photo writes should be attempted at all (LDAP enabled and at
# least one photo attribute configured).  Both publish paths consult this
# single flag so the skip logic cannot drift between them.
LDAP_PHOTOS_ACTIVE = ldap_is_enabled() and bool(_LDAP_PHOTOS)

# Cross-process lockfiles for the background jobs.  Both live in the avatar
# root so every process (Flask app, run_cleanup.py, run_sync_gravatar.py) sees
# the same files regardless of its working directory.
CLEANUP_LOCKFILE = AVATAR_ROOT / ".cleanup.lock"
GRAVATAR_SYNC_LOCKFILE = AVATAR_ROOT / ".gravatar_sync.lock"


def resolve_canonical_url(urls: dict) -> str:
    """
    Look up the canonical avatar URL (the one pushed to Authentik) from the
    generated URL map.  Raises RuntimeError if the expected size/format is
    missing (misconfigured images.sizes / images.formats).
    """
    canonical = urls.get(CANONICAL_SIZE_KEY, {}).get(CANONICAL_FORMAT)
    if not canonical:
        raise RuntimeError(
            f"Canonical avatar URL not found: size={CANONICAL_SIZE_KEY}, "
            f"format={CANONICAL_FORMAT}. Ensure {ak_avatar_size} is in "
            f'images.sizes and "{CANONICAL_FORMAT}" is in images.formats.'
        )
    log.debug("Canonical Authentik avatar URL: %s", canonical)
    return canonical


def build_ldap_updates(
    image: Image.Image, urls: dict, filename_base: str
) -> list[dict]:
    """
    Build LDAP attribute updates from the ``ldap.photos`` config.

    For ``binary`` entries the image is encoded on-the-fly (or reused from disk).
    For ``url`` entries the pre-generated public URL is looked up.
    """
    updates = []
    for photo_cfg in _LDAP_PHOTOS:
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


def save_avatar_metadata(
    filename_base: str,
    user_pk: int,
    total_bytes: int,
    source: str = "web",
    gravatar_hash: str | None = None,
) -> None:
    """
    Persist avatar metadata as JSON.  Uses the Authentik PK (immutable, no PII)
    as the owner identifier for cleanup/retention matching.

    ``source`` records where the avatar came from: ``"web"`` for any web-UI
    action (upload, URL, webcam, in-browser Gravatar import) or
    ``"gravatar_sync"`` for the background sync job.  The Gravatar sync reads
    this field to know which avatars it owns (and may update) versus user-set
    avatars it must never overwrite.  ``gravatar_hash`` (a sha256 of the fetched
    Gravatar image bytes) is written only for ``gravatar_sync`` avatars and is
    used to detect when a user's Gravatar has changed.
    """
    metadata = {
        "filename": filename_base,
        "user_pk": user_pk,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "sizes": img_sizes,
        "formats": img_formats,
        "total_bytes": total_bytes,
        "source": source,
    }
    # Only sync-applied avatars carry a Gravatar hash (used for change detection).
    if gravatar_hash is not None:
        metadata["gravatar_hash"] = gravatar_hash

    meta_path = METADATA_ROOT / f"{filename_base}.meta.json"
    # Atomic publish: write to a sibling .tmp file and os.replace() into place,
    # so a concurrent reader (cleanup, serve_avatar_metadata) never sees a
    # half-written JSON file.  os.replace() is atomic on POSIX and on Windows
    # when the source and destination are on the same filesystem.
    tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, meta_path)
    log.debug("Metadata saved to %s.", meta_path)


def sync_ldap_photos(
    image: Image.Image, urls: dict, filename_base: str, ak_attrs: dict, user_pk: int
) -> bool:
    """
    Write the configured LDAP photo attributes for one user.

    Returns ``True`` when LDAP updates were applied (or suppressed by dry-run
    inside ``update_photos``) and ``False`` when the step was skipped: LDAP is
    disabled / has no photo attributes, or the user has no ``ldap_uniq``
    (Authentik-only users are not synced from LDAP).  Raises on any LDAP
    failure so the caller can roll back.
    """
    if not LDAP_PHOTOS_ACTIVE:
        return False

    ldap_uniq = ak_attrs.get("ldap_uniq", None) if isinstance(ak_attrs, dict) else None
    if not ldap_uniq:
        log.info("User pk=%s has no ldap_uniq - skipping LDAP updates.", user_pk)
        return False

    log.debug(
        "User has ldap_uniq=%r - preparing %d LDAP photo update(s).",
        ldap_uniq,
        len(_LDAP_PHOTOS),
    )
    update_ldap_photos(ldap_uniq, build_ldap_updates(image, urls, filename_base))
    return True


def build_webhook_context(
    user: dict, canonical_url: str, filename_base: str, total_bytes: int
) -> dict:
    """
    Build the placeholder map passed to :func:`src.webhooks.fire_webhooks`.

    ``user`` must contain ``pk`` and ``username``; ``name`` and ``email`` are
    optional.  Both publish paths build their payload here so a new placeholder
    only ever has to be added in one place.
    """
    return {
        "username": user["username"],
        "name": user.get("name", "") or "",
        "email": user.get("email", "") or "",
        "user_pk": user["pk"],
        "avatar_url": canonical_url,
        "avatar_id": filename_base,
        "total_bytes": total_bytes,
        "timestamp": datetime.now(UTC).isoformat(),
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
    }


@contextmanager
def exclusive_process_lock(lockfile_path: Path, job_name: str):
    """
    Context manager that takes a non-blocking exclusive ``flock`` on
    *lockfile_path* for the duration of the block.

    Yields ``True`` when the lock was acquired and ``False`` when another
    process already holds it (or the lockfile could not be opened) - the
    caller then skips its work instead of queueing.  ``job_name`` is only used
    in log messages.  The fd is closed on every exit path, which also releases
    the OS-level lock even if the block raises.
    """
    try:
        lockfile = open(lockfile_path, "w")
    except OSError as exc:
        log.warning("Could not open %s lockfile %s: %s", job_name, lockfile_path, exc)
        yield False
        return

    with lockfile:
        try:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.warning(
                "%s already in progress (another process holds %s) - skipping.",
                job_name,
                lockfile_path.name,
            )
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)
