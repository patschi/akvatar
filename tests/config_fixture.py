"""
config_fixture.py - The config.yml used by the test suite.

``src/config.py`` reads and validates ``config.yml`` at *import* time and calls
``sys.exit(1)`` when anything is wrong, so every test run needs a real config
file on disk before the first ``import src.*`` happens.  This module builds that
file.  It deliberately imports nothing from ``src`` so that ``conftest.py`` can
call it before the application package is touched.

The same builder is reused by ``test_config_validation.py``, which writes
deliberately broken variants of this config into a temp directory and asserts
that starting the app against them fails with a clear FATAL message.
"""

import copy
from pathlib import Path

import yaml

# 64 hex characters - the shape `python3 -c "import secrets; print(secrets.token_hex(32))"`
# produces, and comfortably above the 32-character minimum config.py enforces.
TEST_SECRET_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"

# Public URLs the suite asserts against.  The WebUI and avatar origins are
# deliberately *different* hosts so tests cover the cross-origin img-src branch
# in sec_csp.py and the "avatar URL is not the app URL" branch in imaging.py.
TEST_WEBUI_URL = "https://avatar.test.example.com"
TEST_AVATAR_URL = "https://cdn.test.example.com/user-avatars"

# Image geometry used throughout the suite.  Kept small (largest = 256 px) so a
# full upload through process_image() stays in the low milliseconds, while still
# exercising the multi-size chained downscale.
TEST_SIZES = [256, 128, 64]
TEST_FORMATS = ["jpg", "png", "webp"]

# Authentik's canonical avatar (the single size/format pushed to the user profile).
TEST_AK_AVATAR_SIZE = 256
TEST_AK_AVATAR_FORMAT = "jpg"

# Non-default RGBA composite background (pure red) so tests can prove that
# transparent pixels are flattened onto the *configured* color rather than the
# white default or an accidental black.
TEST_RGBA_BACKGROUND = [255, 0, 0]


def base_config(storage_path: str | Path) -> dict:
    """Return the canonical test configuration as a plain dict.

    ``storage_path`` becomes ``app.avatar_storage_path`` - the directory that
    imaging.py turns into AVATAR_ROOT at import time.
    """
    return {
        "dry_run": False,
        "dry_run_backend": False,
        "app": {
            "public_webui_url": TEST_WEBUI_URL,
            "public_avatar_url": TEST_AVATAR_URL,
            "avatar_storage_path": str(storage_path),
            "max_upload_size_mb": 2,
            # WARNING keeps pytest output readable; individual tests raise the
            # level with caplog when they need to assert on log records.
            "log_level": "WARNING",
            "debug_full": False,
        },
        "branding": {"name": "Test Avatars"},
        "security": {
            "secret_key": TEST_SECRET_KEY,
            # Forced off: the Werkzeug test client speaks plain HTTP and would
            # silently drop a Secure cookie, breaking every authenticated test.
            "session_cookie_secure": False,
            "metadata_access": "owner_only",
            "web_session_lifetime_seconds": 1800,
            "csp_enabled": True,
            "csp_report_only": False,
            "csp_report_uri": "",
        },
        "webserver": {
            "proxy_mode": True,
            # "localhost" is what the test client sends as Host; the two public
            # hostnames are included so the auto-derivation branch stays
            # representative and an unknown Host can be asserted to fail.
            "trusted_hosts": [
                "localhost",
                "avatar.test.example.com",
                "cdn.test.example.com",
            ],
            "access_log": False,
            "http2": {"enabled": False},
        },
        "oidc": {
            "issuer_url": "https://auth.test.example.com/application/o/akvatar",
            "client_id": "akvatar-test-client",
            "client_secret": "test-client-secret",
            "username_claim": "preferred_username",
            # Enabled so the RP-Initiated Logout code path is reachable.
            "end_provider_session": True,
        },
        "authentik": {
            "base_url": "https://auth.test.example.com",
            "api_token": "test-api-token",
            "avatar_attribute": "avatar",
            "avatar_id_attribute": "avatar_id",
            "avatar_size": TEST_AK_AVATAR_SIZE,
            "avatar_format": TEST_AK_AVATAR_FORMAT,
        },
        "images": {
            "sizes": list(TEST_SIZES),
            "formats": list(TEST_FORMATS),
            "jpeg_quality": 80,
            "webp_quality": 75,
            # Lowest compression: PNG encoding dominates process_image() runtime
            # and the tests never assert on output file size.
            "png_compress_level": 1,
            "rgba_background_color": list(TEST_RGBA_BACKGROUND),
        },
        "ldap": {
            # Enabled (against unreachable .invalid hosts) so LDAP_PHOTOS_ACTIVE
            # is True and the pipeline's LDAP branch is exercised.  Every test
            # that reaches the wire stubs out ldap_client.update_photos.
            "enabled": True,
            "servers": "ldaps://ldap-a.test.invalid,ldap://ldap-b.test.invalid:389",
            "port": 636,
            "use_ssl": True,
            "bind_dn": "CN=svc,DC=test,DC=invalid",
            "bind_password": "ldap-secret",
            "search_base": "DC=test,DC=invalid",
            "search_filter": "(objectSid={ldap_uniq})",
            "photos": [
                {
                    "attribute": "thumbnailPhoto",
                    "type": "binary",
                    "image_type": "jpeg",
                    "image_size": 128,
                    "max_file_size": 100,
                },
                {
                    "attribute": "photoURL",
                    "type": "url",
                    "image_type": "webp",
                    "image_size": 64,
                },
            ],
        },
        "cleanup": {
            # Empty schedule + no startup run: importing src.cleanup must not
            # spawn a background thread during the test session.
            "interval": "",
            "on_startup": False,
            "avatar_retention_count": 2,
            "when_user_deleted": True,
            "when_user_deactivated": False,
            "scheduler_priority": 0,
            "backfill_missing_images": True,
        },
        # Disabled so importing src.rate_limit does not start a
        # multiprocessing.Manager server process for the whole session.  The
        # limiter internals are unit-tested directly with a stub Manager.
        "rate_limiting": {"enabled": False},
        "image_import": {
            "gravatar": {"enabled": True, "restrict_email": True},
            "url": {"enabled": True, "restrict_private_ips": True},
            "webcam": {"enabled": True},
        },
        "webhooks": {
            "enabled": True,
            "endpoints": [
                {
                    "name": "test-hook",
                    "url": "https://hooks.test.invalid/avatar",
                    "method": "POST",
                    "timeout": 5,
                    "headers": {"X-Akvatar-User": "{username}"},
                    "body": {
                        "event": "avatar.updated",
                        "user_pk": "{user_pk}",
                        "avatar_url": "{avatar_url}",
                        "total_bytes": "{total_bytes}",
                        "note": "avatar for {username} ({email})",
                    },
                }
            ],
        },
        "sentry": {"enabled": False},
    }


def write_config(path: str | Path, config: dict) -> Path:
    """Serialize *config* to YAML at *path* and return the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return target


def config_with(storage_path: str | Path, **section_overrides) -> dict:
    """Return the base config with whole top-level sections deep-merged.

    Each keyword is a top-level config section; dict values are merged key by
    key into the base section, anything else replaces it outright.  ``None``
    removes the section entirely, which is how tests reproduce "operator forgot
    to configure X".
    """
    config = base_config(storage_path)
    for section, override in section_overrides.items():
        if override is None:
            config.pop(section, None)
        elif isinstance(override, dict) and isinstance(config.get(section), dict):
            merged = copy.deepcopy(config[section])
            merged.update(override)
            config[section] = merged
        else:
            config[section] = override
    return config
