"""
app_monitor.py – Periodic process memory monitor.

Logs the process RSS (Resident Set Size) in MB every few seconds, but only
when the value has changed.  Runs in a daemon thread so it exits automatically
when the main process shuts down.
"""

import logging
import threading
import time

log = logging.getLogger('app')


def _get_rss_mb() -> float | None:
    """Return current process RSS in MB, or None if unavailable."""
    # Linux: parse VmRSS from /proc/self/status (value in KB)
    try:
        with open('/proc/self/status') as f:
            line = next(l for l in f if l.startswith('VmRSS:'))
        return int(line.split()[1]) / 1024
    except Exception:
        return None


def _memory_log_loop() -> None:
    """Log process RSS every few seconds, but only when the value has changed."""
    last_mem = None
    while True:
        mem = _get_rss_mb()
        if mem is not None and mem != last_mem:
            log.debug('Monitor: Process memory: %.1f MB', mem)
            last_mem = mem
        time.sleep(5)


def start_memory_monitor() -> None:
    """Start the memory monitor thread (once)."""
    threading.Thread(target=_memory_log_loop, name='memlog', daemon=True).start()
    log.debug('Memory monitor thread started.')
