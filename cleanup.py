"""
cleanup.py – Manual trigger for avatar cleanup.

Run with:  python cleanup.py

The actual logic lives in src/cleanup.py.  This file is a convenience
entry point for one-off manual runs (the same function also runs
automatically in a background thread when the app is running).
"""

import os
import sys

# Prevent .pyc file clutter.
# sys.dont_write_bytecode must be set here (before imports) to suppress bytecode
# in this process.  os.environ is set so gunicorn and any subprocesses it spawns
# inherit the setting without needing their own flag.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

# Ensure immediate log output (no buffering)
os.environ.setdefault('PYTHONUNBUFFERED', '1')

from src.cleanup import run_cleanup

if __name__ == '__main__':
    run_cleanup()
    sys.exit(0)
