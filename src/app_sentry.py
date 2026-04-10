"""
app_sentry.py – Sentry SDK initialisation.

Called once at application startup before Flask is created, so the SDK can
hook into framework internals.  When disabled or missing a DSN this module
is a no-op and sentry-sdk is never imported.
"""

import logging

from src import APP_NAME, APP_VERSION
from src.config import debug_full, sentry_cfg

log = logging.getLogger("app.sentry")


def init_sentry() -> None:
    """Initialise the Sentry SDK from config.  No-op when disabled or DSN is empty."""
    if not sentry_cfg.get("enabled", False):
        return

    dsn = sentry_cfg.get("dsn", "")
    if not dsn:
        log.warning(
            "Sentry is enabled but no DSN is configured – skipping initialisation."
        )
        return

    import sentry_sdk

    # auto-detect environment from debug_full when not explicitly configured
    environment = sentry_cfg.get("environment", "") or (
        "development" if debug_full else "production"
    )

    # disable performance tracing entirely when capture_performance is off
    capture_performance = sentry_cfg.get("capture_performance", False)
    traces_sample_rate = (
        sentry_cfg.get("traces_sample_rate", 0.2) if capture_performance else 0.0
    )

    # allow disabling error capture while keeping performance tracing
    capture_errors = sentry_cfg.get("capture_errors", True)
    sample_rate = sentry_cfg.get("sample_rate", 1.0) if capture_errors else 0.0

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=f"{APP_NAME}@{APP_VERSION}",
        sample_rate=sample_rate,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=sentry_cfg.get("send_default_pii", False),
        # attach the Flask integration automatically (provided by sentry-sdk[flask])
        enable_tracing=capture_performance,
    )

    log.info(
        "Sentry initialised (env=%s, errors=%s, performance=%s).",
        environment,
        capture_errors,
        capture_performance,
    )
