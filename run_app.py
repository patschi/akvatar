"""
run_app.py - Production entrypoint (gunicorn).

Reads webserver settings from config.yml (host, port, workers, TLS) and
launches gunicorn with the appropriate arguments.  The cleanup thread is
started here in the master process so it runs exactly once - gunicorn
workers are forked afterwards and do not duplicate it.

Can be used both inside Docker and directly on the host:
    python run_app.py

A Python script is used instead of a shell script because the distroless
container image has no shell.
"""

import logging
import os
import sys
import tempfile

# Prevent .pyc file clutter.
# sys.dont_write_bytecode must be set here (before imports) to suppress bytecode
# in this process.  os.environ is set so gunicorn and any subprocesses it spawns
# inherit the setting without needing their own flag.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

# Ensure immediate log output (no buffering)
os.environ.setdefault("PYTHONUNBUFFERED", "1")

# Gunicorn 23+ creates a control-server socket under ~/.gunicorn/.
# The container runs with a read-only root filesystem, so /home/nonroot is not
# writable.  Redirect HOME to /tmp (already a tmpfs mount) so gunicorn can
# write its socket there without error.
os.environ["HOME"] = "/tmp"

from src.cleanup import start_cleanup_thread  # noqa: E402
from src.config import (  # noqa: E402
    http2_enabled,
    tls_cert,
    tls_configured,
    tls_key,
    tls_minimum_version,
    web_host,
    web_port,
    web_threads,
    web_timeout,
    web_workers,
)

log = logging.getLogger("run_app")

scheme = "https" if tls_configured else "http"

# HTTP/2 is active when both the config option is enabled and TLS is configured.
# gunicorn negotiates HTTP/2 via ALPN during the TLS handshake when h2 is installed.
http2_active = http2_enabled and tls_configured

# Start the cleanup thread in the master process before gunicorn forks
start_cleanup_thread()

# Build gunicorn command-line arguments
workers = web_workers
threads = web_threads
timeout = web_timeout
wtmpdir = tempfile.gettempdir()

# fmt: off
args = [
    "gunicorn",
    "--no-control-socket",
    "--preload",
    "--bind", f"{web_host}:{web_port}",
    "--worker-class", "gthread",
    "--worker-tmp-dir", wtmpdir,
    "--workers", str(workers),
    "--threads", str(threads),
    "--timeout", str(timeout),
]
# fmt: on

if tls_cert and tls_key:
    args.extend(["--certfile", tls_cert, "--keyfile", tls_key])

if http2_active:
    # Advertise h2 alongside http/1.1 via ALPN so clients can negotiate HTTP/2
    args.extend(["--http-protocols", "h2,h1"])
elif http2_enabled and not tls_configured:
    log.warning(
        "HTTP/2 is enabled in config but TLS is not configured - HTTP/2 requires TLS."
    )

args.append("app:create_app()")

log.info(
    "Initializing gunicorn on %s://%s:%s (workers=%d, threads=%d, timeout=%ds)...",
    scheme,
    web_host,
    web_port,
    workers,
    threads,
    timeout,
)

# Override gunicorn's Server header before it is imported by wsgiapp.
# gunicorn always injects SERVER_SOFTWARE as a default header after Flask
# returns the response, so the Flask after_request hook cannot remove it.
# Patching the module-level variable here (before wsgiapp triggers the import)
# replaces it with the app name instead.
import gunicorn.http.wsgi  # noqa: E402

from src import APP_NAME  # noqa: E402

gunicorn.http.wsgi.SERVER = APP_NAME

# Launch gunicorn in-process.
# Split WSGIApplication construct + configure + run so we can inject the
# when_ready hook before the server starts accepting connections.
sys.argv = args
from gunicorn.app.wsgiapp import WSGIApplication  # noqa: E402


def _when_ready(server):
    log.info("OK! Ready to serve requests.")


gunicorn_app = WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]")
gunicorn_app.cfg.set("when_ready", _when_ready)

if tls_configured:
    # Enforce the configured minimum TLS version on the server SSL context.
    # The default factory is called first (it loads cert/key and applies gunicorn's
    # standard options), then minimum_version is set before returning.
    _min_ver = tls_minimum_version

    def _ssl_context(conf, default_ssl_context_factory):
        ctx = default_ssl_context_factory()
        ctx.minimum_version = _min_ver
        return ctx

    gunicorn_app.cfg.set("ssl_context", _ssl_context)
    log.info("TLS minimum version enforced: %s.", tls_minimum_version.name)

if http2_active:
    log.info("HTTP/2 active: gunicorn will advertise h2 via ALPN.")

gunicorn_app.run()
