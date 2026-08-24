"""
run_sync_gravatar.py - Manual one-time Gravatar sync/backfill.

Run inside the container (or on the host) with:

    python run_sync_gravatar.py                     # active users only
    python run_sync_gravatar.py --include-disabled  # also deactivated users

Walks Authentik users, fetches each one's Gravatar image, and imports it
through the same pipeline as a manual upload.  Re-running updates avatars whose
Gravatar changed and imports any newly eligible users.  Avatars a user set
themselves are never overwritten.

The actual logic lives in src/gravatar_sync.py.
"""

import argparse
import os
import sys

# Prevent .pyc file clutter.
# sys.dont_write_bytecode must be set here (before imports) to suppress bytecode
# in this process.  os.environ is set so any subprocesses inherit the setting.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

# Ensure immediate log output (no buffering)
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from src.gravatar_sync import run_gravatar_sync  # noqa: E402
from src.imaging import ensure_size_directories_existence  # noqa: E402

# Exit codes so cron/CI can alert on a run that did not fully succeed.
EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1  # the run completed but at least one user failed
EXIT_ABORTED = 2  # nothing ran: lock held, user list unavailable, or zero users


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_sync_gravatar.py",
        description=(
            "One-time Gravatar backfill/sync for Authentik users. Imports each "
            "user's Gravatar avatar through the same pipeline as a manual "
            "upload; re-running updates avatars whose Gravatar changed. A "
            "user-set avatar is never overwritten."
        ),
    )
    parser.add_argument(
        "--include-deactivated",
        "--include-disabled",
        dest="include_deactivated",
        action="store_true",
        help="Also process deactivated/disabled users (default: active users only).",
    )
    parser.add_argument(
        "--fire-webhooks",
        dest="fire_webhooks",
        action="store_true",
        help="Fire configured webhooks for each synced avatar (default: off for bulk runs).",
    )
    parser.add_argument(
        "--delay-ms",
        dest="delay_ms",
        type=int,
        default=0,
        metavar="MS",
        help="Pause this many milliseconds between users to throttle Gravatar load (default: 0).",
    )
    args = parser.parse_args()

    # Ensure the avatar root and metadata directory exist before running.
    # When run standalone (outside the Flask app) these may not exist yet.
    ensure_size_directories_existence()
    counts = run_gravatar_sync(
        include_deactivated=args.include_deactivated,
        fire_webhooks_enabled=args.fire_webhooks,
        request_delay_ms=args.delay_ms,
    )
    # An empty result means the run never started (details are already logged).
    if not counts:
        return EXIT_ABORTED
    if counts.get("failed", 0) > 0:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
