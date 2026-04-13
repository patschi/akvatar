"""
app_sentry.py - Sentry SDK initialization.

Called once at application startup before Flask is created, so the SDK can
hook into framework internals.  When disabled or missing a DSN this module
is a no-op and sentry-sdk is never imported.
"""

import logging

from src import APP_NAME, APP_VERSION
from src.config import (
    debug_full,
    sentry_browser_dsn,
    sentry_browser_enabled,
    sentry_browser_js_sdk_url,
    sentry_browser_sample_rate,
    sentry_browser_traces_sample_rate,
    sentry_browser_tunnel_enabled,
    sentry_capture_errors,
    sentry_capture_performance,
    sentry_dsn,
    sentry_enabled,
    sentry_environment,
    sentry_sample_rate,
    sentry_send_default_pii,
    sentry_traces_sample_rate,
)

log = logging.getLogger("app.sentry")

# Auto-detect environment from debug_full when not explicitly configured
_environment = sentry_environment or ("development" if debug_full else "production")


def init_sentry() -> None:
    """Initialize the Sentry SDK from config.  No-op when disabled or DSN is empty."""
    if not sentry_enabled:
        return

    if not sentry_dsn:
        log.warning(
            "Sentry is enabled but no DSN is configured - skipping initialization."
        )
        return

    import sentry_sdk

    # disable performance tracing entirely when capture_performance is off
    traces_sample_rate = (
        sentry_traces_sample_rate if sentry_capture_performance else 0.0
    )

    # allow disabling error capture while keeping performance tracing
    sample_rate = sentry_sample_rate if sentry_capture_errors else 0.0

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=_environment,
        release=f"{APP_NAME}@{APP_VERSION}",
        sample_rate=sample_rate,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=sentry_send_default_pii,
        # attach the Flask integration automatically (provided by sentry-sdk[flask])
        enable_tracing=sentry_capture_performance,
    )

    log.info(
        "Sentry initialized (env=%s, errors=%s, performance=%s).",
        _environment,
        sentry_capture_errors,
        sentry_capture_performance,
    )


def get_browser_sentry_config() -> dict | None:
    """Return a JSON-serializable dict of browser Sentry settings for templates.

    Returns ``None`` when browser-side Sentry is disabled so templates can
    use a simple ``{% if sentry_browser %}`` guard.
    """
    if not sentry_browser_enabled:
        return None

    if not sentry_browser_js_sdk_url:
        log.warning(
            "Browser Sentry is enabled but no JS SDK URL is configured "
            "(sentry.browser.js_sdk_url) - skipping browser integration."
        )
        return None

    if not sentry_browser_dsn:
        log.warning(
            "Browser Sentry is enabled but no DSN is configured "
            "(sentry.browser.dsn / sentry.dsn) - skipping browser integration."
        )
        return None

    config = {
        "enabled": True,
        "js_sdk_url": sentry_browser_js_sdk_url,
        "dsn": sentry_browser_dsn,
        "environment": _environment,
        "release": f"{APP_NAME}@{APP_VERSION}",
        "sample_rate": sentry_browser_sample_rate,
        "traces_sample_rate": sentry_browser_traces_sample_rate,
        "tunnel_enabled": sentry_browser_tunnel_enabled,
    }

    log.info(
        "Browser Sentry enabled (env=%s, tunnel=%s).",
        _environment,
        "on" if sentry_browser_tunnel_enabled else "off",
    )
    return config
