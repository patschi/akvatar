"""
cleanup.py – Manual trigger for avatar cleanup.

Run with:  python cleanup.py

The actual logic lives in src/cleanup.py.  This file is a convenience
entry point for one-off manual runs (the same function also runs
automatically in a background thread when the app is running).
"""

import sys

from src.cleanup import run_cleanup

if __name__ == '__main__':
    run_cleanup()
    sys.exit(0)
