"""
run.py – Production entrypoint (used by Docker / gunicorn).

Reads webserver settings from config.yml (host, port, workers, TLS) and
launches gunicorn with the appropriate arguments.  The cleanup thread is
started here in the master process so it runs exactly once — gunicorn
workers are forked afterwards and do not duplicate it.

A Python script is used instead of a shell script because the distroless
container image has no shell.
"""

import sys
import logging

from src.config import web_cfg
from src.cleanup import start_cleanup_thread

log = logging.getLogger('run')

host = web_cfg.get('host', '0.0.0.0')
port = web_cfg.get('port', 5000)
workers = web_cfg.get('workers', 2)
tls_cert = web_cfg.get('tls_cert', '')
tls_key = web_cfg.get('tls_key', '')
scheme = 'https' if tls_cert and tls_key else 'http'

# Start the cleanup thread in the master process before gunicorn forks
start_cleanup_thread()

# Build gunicorn command-line arguments
args = [
    'gunicorn',
    '--preload',
    '--bind', f'{host}:{port}',
    '--workers', str(workers),
    '--worker-tmp-dir', '/tmp',
    '--access-logfile', '-',
]

if tls_cert and tls_key:
    args.extend(['--certfile', tls_cert, '--keyfile', tls_key])

args.append('app:create_app()')

log.info('Starting gunicorn on %s://%s:%s (workers=%d)...', scheme, host, port, workers)

# Launch gunicorn in-process (replaces the current Python execution context)
sys.argv = args
from gunicorn.app.wsgiapp import run
run()
