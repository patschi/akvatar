"""
run_app.py – Production entrypoint (gunicorn).

Reads webserver settings from config.yml (host, port, workers, TLS) and
launches gunicorn with the appropriate arguments.  The cleanup thread is
started here in the master process so it runs exactly once — gunicorn
workers are forked afterwards and do not duplicate it.

Can be used both inside Docker and directly on the host:
    python run_app.py

A Python script is used instead of a shell script because the distroless
container image has no shell.
"""

import os
import sys
import logging
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

from src.config import web_cfg  # noqa: E402
from src.cleanup import start_cleanup_thread  # noqa: E402

log = logging.getLogger("run_app")

host = web_cfg.get("host", "0.0.0.0")
port = web_cfg.get("port", 5000)
tls_cert = web_cfg.get("tls_cert", "")
tls_key = web_cfg.get("tls_key", "")
scheme = "https" if tls_cert and tls_key else "http"

# Start the cleanup thread in the master process before gunicorn forks
start_cleanup_thread()

# Build gunicorn command-line arguments
workers = web_cfg.get("workers", 2)
threads = web_cfg.get("threads", 4)
timeout = web_cfg.get("timeout", 120)
wtmpdir = tempfile.gettempdir()

args = [
    "gunicorn",
    "--preload",
    "--bind",
    f"{host}:{port}",
    "--workers",
    str(workers),
    "--threads",
    str(threads),
    "--worker-class",
    "gthread",
    "--worker-tmp-dir",
    wtmpdir,
    "--timeout",
    str(timeout),
]

if tls_cert and tls_key:
    args.extend(["--certfile", tls_cert, "--keyfile", tls_key])

args.append("app:create_app()")

log.info(
    "Initializing gunicorn on %s://%s:%s (workers=%d, threads=%d, timeout=%ds)...",
    scheme,
    host,
    port,
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
gunicorn_app.run()
