"""
run.py – Production entrypoint (gunicorn).

Reads webserver settings from config.yml (host, port, workers, TLS) and
launches gunicorn with the appropriate arguments.  The cleanup thread is
started here in the master process so it runs exactly once — gunicorn
workers are forked afterwards and do not duplicate it.

Can be used both inside Docker and directly on the host:
    python run.py

A Python script is used instead of a shell script because the distroless
container image has no shell.
"""

import os
import sys
import logging
import tempfile

# Prevent .pyc file clutter and ensure immediate log output
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
os.environ.setdefault('PYTHONUNBUFFERED', '1')

from src.config import web_cfg
from src.cleanup import start_cleanup_thread

log = logging.getLogger('run')

host = web_cfg.get('host', '0.0.0.0')
port = web_cfg.get('port', 5000)
tls_cert = web_cfg.get('tls_cert', '')
tls_key = web_cfg.get('tls_key', '')
scheme = 'https' if tls_cert and tls_key else 'http'

# Start the cleanup thread in the master process before gunicorn forks
start_cleanup_thread()

# Build gunicorn command-line arguments
workers = web_cfg.get('workers', 2)
threads = web_cfg.get('threads', 4)
timeout = web_cfg.get('timeout', 120)
wtmpdir = tempfile.gettempdir()

args = [
    'gunicorn',
    '--preload',
    '--bind', f'{host}:{port}',
    '--workers', str(workers),
    '--threads', str(threads),
    '--worker-class', 'gthread',
    '--worker-tmp-dir', wtmpdir,
    '--timeout', str(timeout),
]

if tls_cert and tls_key:
    args.extend(['--certfile', tls_cert, '--keyfile', tls_key])

args.append('app:create_app()')

log.info('Starting gunicorn on %s://%s:%s (workers=%d, threads=%d, timeout=%ds)...',
         scheme, host, port, workers, threads, timeout)

# Launch gunicorn in-process (replaces the current Python execution context)
sys.argv = args
from gunicorn.app.wsgiapp import run
run()
