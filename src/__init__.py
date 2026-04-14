# src package - all application modules live here.

import os

APP_NAME = "akvatar"
APP_BASE_VERSION = "0.9.0"

# Git short hash injected at Docker build time via the APP_GIT_HASH env var.
# Falls back to "unknown" when running outside Docker (e.g. local dev without --build-arg).
APP_VERSION = f"{APP_BASE_VERSION}+{os.environ.get('APP_GIT_HASH', 'unknown')}"

USER_AGENT = f"{APP_NAME}/v{APP_VERSION}"
