"""Tests for src/config.py - startup configuration validation.

``src/config.py`` validates config.yml at import time and calls ``sys.exit(1)``
with a FATAL message on anything it cannot accept.  That is by far the largest
"fails fast at startup" surface in the application, and it is unreachable from
an in-process test: the module is already imported, and re-importing it would
tear down every module that captured its values.

Each case here therefore boots a fresh interpreter against a purpose-built
config file and asserts on the exit code and the FATAL message.  Marked
``slow`` so ``-m "not slow"`` gives a fast inner-loop run.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.config_fixture import (
    TEST_SECRET_KEY,
    base_config,
    config_with,
    write_config,
)

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config(tmp_path: Path, config: dict) -> subprocess.CompletedProcess:
    """Import src.config in a fresh interpreter against *config*.

    Returns the completed process so tests can assert on the exit code and the
    FATAL message written to stderr.
    """
    config_path = write_config(tmp_path / "config.yml", config)
    return subprocess.run(
        [sys.executable, "-c", "import src.config"],
        cwd=REPO_ROOT,
        env={
            "CONFIG_PATH": str(config_path),
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def assert_fatal(result: subprocess.CompletedProcess, *expected_fragments: str) -> None:
    """Assert the run aborted with a FATAL message containing every fragment."""
    assert result.returncode == 1, (
        f"expected a FATAL exit, got {result.returncode}\n{result.stderr}"
    )
    assert "FATAL:" in result.stderr, result.stderr
    for fragment in expected_fragments:
        assert fragment in result.stderr, f"missing {fragment!r} in:\n{result.stderr}"


def assert_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (
        f"expected a clean start, got {result.returncode}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_the_reference_config_starts_cleanly(tmp_path):
    assert_ok(load_config(tmp_path, base_config(tmp_path / "avatars")))


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


def test_a_missing_config_file_is_fatal(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import src.config"],
        cwd=REPO_ROOT,
        env={
            "CONFIG_PATH": str(tmp_path / "nope.yml"),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert_fatal(result, "not found")


def test_malformed_yaml_is_fatal(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("app: {unclosed", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", "import src.config"],
        cwd=REPO_ROOT,
        env={
            "CONFIG_PATH": str(config_path),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert_fatal(result, "Failed to parse")


# ---------------------------------------------------------------------------
# Required public URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["public_webui_url", "public_avatar_url"])
def test_a_missing_public_url_is_fatal(tmp_path, key):
    config = base_config(tmp_path / "avatars")
    config["app"].pop(key)
    assert_fatal(load_config(tmp_path, config), f"app.{key} is required")


@pytest.mark.parametrize("key", ["public_webui_url", "public_avatar_url"])
def test_a_relative_public_url_is_fatal(tmp_path, key):
    config = base_config(tmp_path / "avatars")
    config["app"][key] = "/avatars"
    assert_fatal(load_config(tmp_path, config), "must be an absolute URL")


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------


def test_the_placeholder_secret_key_is_fatal(tmp_path):
    result = load_config(
        tmp_path,
        config_with(
            tmp_path / "avatars",
            security={"secret_key": "CHANGE-ME-to-a-random-secret-key"},
        ),
    )
    assert_fatal(result, "default placeholder value")


def test_a_short_secret_key_is_fatal(tmp_path):
    result = load_config(
        tmp_path, config_with(tmp_path / "avatars", security={"secret_key": "short"})
    )
    assert_fatal(result, "too short")


def test_a_secret_key_at_exactly_the_minimum_length_is_accepted(tmp_path):
    assert_ok(
        load_config(
            tmp_path,
            config_with(tmp_path / "avatars", security={"secret_key": "x" * 32}),
        )
    )


# ---------------------------------------------------------------------------
# Image geometry
# ---------------------------------------------------------------------------


def test_an_unsupported_image_format_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["images"]["formats"] = ["jpg", "bmp"]
    assert_fatal(load_config(tmp_path, config), "unsupported format", "'bmp'")


def test_a_typo_in_the_format_list_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["images"]["formats"] = ["jpge"]
    assert_fatal(load_config(tmp_path, config), "unsupported format")


def test_the_authentik_avatar_size_must_be_generated(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["authentik"]["avatar_size"] = 4096
    assert_fatal(load_config(tmp_path, config), "authentik.avatar_size=4096")


def test_the_authentik_avatar_format_must_be_generated(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["images"]["formats"] = ["png"]
    config["authentik"]["avatar_format"] = "jpg"
    assert_fatal(load_config(tmp_path, config), "is not in images.formats")


def test_an_invalid_authentik_avatar_format_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["authentik"]["avatar_format"] = "tiff"
    assert_fatal(load_config(tmp_path, config), "is not a valid format")


def test_jpeg_and_jpg_are_treated_as_one_output_format(tmp_path):
    # Otherwise the cleanup job would delete .jpeg files as obsolete-format
    # orphans and the backfill phase would regenerate them, on every run.
    config = base_config(tmp_path / "avatars")
    # webp stays in the list because the LDAP url photo entry references it.
    config["images"]["formats"] = ["jpeg", "jpg", "png", "webp"]
    result = load_config(tmp_path, config)
    assert_ok(result)

    probe = subprocess.run(
        [sys.executable, "-c", "import src.config; print(src.config.img_formats)"],
        cwd=REPO_ROOT,
        env={
            "CONFIG_PATH": str(tmp_path / "config.yml"),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    # "jpeg" and "jpg" collapse into one canonical extension, order preserved.
    assert probe.stdout.strip() == "['jpg', 'png', 'webp']"


@pytest.mark.parametrize(
    "value",
    [
        [255, 255],
        [255, 255, 255, 255],
        "white",
        [300, 0, 0],
        [-1, 0, 0],
        ["a", "b", "c"],
    ],
)
def test_an_invalid_rgba_background_color_is_fatal(tmp_path, value):
    config = base_config(tmp_path / "avatars")
    config["images"]["rgba_background_color"] = value
    assert_fatal(load_config(tmp_path, config), "rgba_background_color")


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def test_a_missing_tls_certificate_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webserver"]["tls"] = {"cert": str(tmp_path / "nope.pem"), "key": "x"}
    assert_fatal(load_config(tmp_path, config), "does not exist or is not a file")


def test_an_invalid_minimum_tls_version_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webserver"]["tls"] = {"min_version": "TLSv9"}
    assert_fatal(load_config(tmp_path, config), "is not a valid TLS version")


def test_a_valid_minimum_tls_version_is_accepted(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webserver"]["tls"] = {"min_version": "TLSv1_3"}
    assert_ok(load_config(tmp_path, config))


# ---------------------------------------------------------------------------
# LDAP photo entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["attribute", "type", "image_type", "image_size"])
def test_an_incomplete_ldap_photo_entry_is_fatal(tmp_path, key):
    config = base_config(tmp_path / "avatars")
    config["ldap"]["photos"][0].pop(key)
    assert_fatal(load_config(tmp_path, config), f'missing required key "{key}"')


def test_an_unknown_ldap_photo_type_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["ldap"]["photos"][0]["type"] = "magic"
    assert_fatal(load_config(tmp_path, config), 'must be "binary" or "url"')


def test_an_unknown_ldap_image_type_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["ldap"]["photos"][0]["image_type"] = "bmp"
    assert_fatal(load_config(tmp_path, config), "image_type=")


def test_a_url_photo_referencing_an_ungenerated_size_is_fatal(tmp_path):
    # type=url points at a pre-generated file, so the size must actually exist.
    config = base_config(tmp_path / "avatars")
    config["ldap"]["photos"][1]["image_size"] = 4096
    assert_fatal(load_config(tmp_path, config), "required for type=url")


def test_a_url_photo_referencing_an_ungenerated_format_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["ldap"]["photos"][1]["image_type"] = "png"
    config["images"]["formats"] = ["jpg", "webp"]
    assert_fatal(load_config(tmp_path, config), "required for type=url")


def test_a_binary_photo_may_use_any_size_and_format(tmp_path):
    # Binary attributes are encoded on the fly, so they are not constrained.
    config = base_config(tmp_path / "avatars")
    config["ldap"]["photos"][0].update({"image_size": 999, "image_type": "png"})
    assert_ok(load_config(tmp_path, config))


def test_ldap_photo_validation_is_skipped_when_ldap_is_disabled(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["ldap"]["enabled"] = False
    config["ldap"]["photos"] = [{"totally": "broken"}]
    assert_ok(load_config(tmp_path, config))


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def test_a_webhook_without_a_url_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0].pop("url")
    assert_fatal(load_config(tmp_path, config), 'missing required key "url"')


def test_a_relative_webhook_url_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["url"] = "/hook"
    assert_fatal(load_config(tmp_path, config), "must be an absolute http(s) URL")


def test_a_non_http_webhook_scheme_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["url"] = "ftp://hooks.invalid/x"
    assert_fatal(load_config(tmp_path, config), "must be an absolute http(s) URL")


def test_an_unsupported_webhook_method_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["method"] = "DELETE"
    assert_fatal(load_config(tmp_path, config), "must be one of")


def test_a_non_positive_webhook_timeout_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["timeout"] = 0
    assert_fatal(load_config(tmp_path, config), "must be a positive integer")


def test_non_mapping_webhook_headers_are_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["headers"] = ["X-Foo: bar"]
    assert_fatal(load_config(tmp_path, config), "must be a mapping")


def test_an_unknown_webhook_placeholder_is_fatal(tmp_path):
    # Catching this at startup avoids a KeyError inside a background delivery
    # thread that the operator would only ever see in the logs.
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["body"] = {"who": "{not_a_real_token}"}
    assert_fatal(
        load_config(tmp_path, config), "unknown placeholder", "not_a_real_token"
    )


def test_an_unknown_placeholder_in_a_header_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["headers"] = {"X-Who": "{nope}"}
    assert_fatal(load_config(tmp_path, config), "unknown placeholder")


def test_a_malformed_webhook_template_is_fatal(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["body"] = {"a": "{unbalanced"}
    assert_fatal(load_config(tmp_path, config), "malformed template")


def test_nested_webhook_placeholders_are_validated(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["body"] = {
        "outer": {"inner": ["{username}", "{bogus}"]}
    }
    assert_fatal(load_config(tmp_path, config), "unknown placeholder", "bogus")


def test_webhook_validation_is_skipped_when_webhooks_are_disabled(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["enabled"] = False
    config["webhooks"]["endpoints"][0]["url"] = "not a url"
    assert_ok(load_config(tmp_path, config))


# ---------------------------------------------------------------------------
# Tolerated / corrected values
# ---------------------------------------------------------------------------


def test_an_unknown_metadata_access_mode_falls_back_instead_of_aborting(tmp_path):
    result = load_config(
        tmp_path,
        config_with(tmp_path / "avatars", security={"metadata_access": "everyone"}),
    )
    assert_ok(result)
    assert "falling back to owner_only" in result.stderr


def test_omitted_optional_sections_fall_back_to_defaults(tmp_path):
    config = base_config(tmp_path / "avatars")
    for section in (
        "ldap",
        "webhooks",
        "sentry",
        "rate_limiting",
        "cleanup",
        "branding",
    ):
        config.pop(section, None)
    assert_ok(load_config(tmp_path, config))


def test_trusted_hosts_are_derived_from_the_public_urls_when_omitted(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webserver"].pop("trusted_hosts")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import src.config; print(src.config.trusted_hosts)",
        ],
        cwd=REPO_ROOT,
        env={
            "CONFIG_PATH": str(write_config(tmp_path / "config.yml", config)),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert "avatar.test.example.com" in probe.stdout
    assert "cdn.test.example.com" in probe.stdout


def test_insecure_settings_produce_warnings_not_failures(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["authentik"]["skip_cert_verify"] = True
    config["oidc"]["skip_cert_verify"] = True
    config["ldap"]["skip_cert_verify"] = True

    result = load_config(tmp_path, config)

    assert_ok(result)
    assert result.stderr.count("verification is DISABLED") >= 3


def test_a_plain_http_webhook_url_warns_about_unencrypted_delivery(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["webhooks"]["endpoints"][0]["url"] = "http://hooks.test.invalid/avatar"

    result = load_config(tmp_path, config)

    assert_ok(result)
    assert "unencrypted" in result.stderr


def test_a_non_https_sentry_sdk_url_warns(tmp_path):
    config = base_config(tmp_path / "avatars")
    config["sentry"] = {
        "enabled": True,
        "dsn": "https://key@sentry.test.invalid/1",
        "browser": {
            "enabled": True,
            "js_sdk_url": "http://cdn.test.invalid/sentry.js",
        },
    }

    result = load_config(tmp_path, config)

    assert_ok(result)
    assert "not HTTPS" in result.stderr


# ---------------------------------------------------------------------------
# Shipped example configurations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example", ["config.example-minimal.yml", "config.example-full.yml"]
)
def test_the_shipped_example_configs_boot(tmp_path, example):
    """The documented starting points must actually start the application.

    Only the values the examples explicitly tell the operator to replace are
    filled in; everything else is used exactly as shipped.
    """
    source = REPO_ROOT / "data" / "config" / example
    config = yaml.safe_load(source.read_text(encoding="utf-8"))

    config.setdefault("security", {})["secret_key"] = TEST_SECRET_KEY
    config.setdefault("app", {})["avatar_storage_path"] = str(tmp_path / "avatars")
    # The full example enables optional integrations against placeholder hosts;
    # none of them are contacted at import time, so no further edits are needed.

    assert_ok(load_config(tmp_path, config))
