"""
rate_limit.py – Per-process, thread-safe, sliding-window rate limiter.

Tracks request timestamps per client IP and returns HTTP 429 when limits are
exceeded.  Each gunicorn worker process maintains its own counters (no shared
state across processes), so with N workers a client can make up to
N × max_requests before every worker blocks them.

A background daemon thread periodically cleans up stale tracking entries to
prevent unbounded memory growth.
"""

import ipaddress
import json
import logging
import threading
import time

from flask import Flask, Response, request

from src.config import cfg

log = logging.getLogger('ratelimit')


# ---------------------------------------------------------------------------
# Sliding-window counter for a single endpoint type
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Track per-IP request timestamps with a fixed-size sliding window."""

    def __init__(self, max_requests: int, window: int) -> None:
        self._max_requests = max_requests
        self._window = window
        # {ip: [monotonic_timestamp, ...]}
        self._requests: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # -- hot path ----------------------------------------------------------

    def check(self, ip: str) -> tuple[bool, int]:
        """
        Record a request from *ip* and test the limit.

        Returns (allowed, retry_after):
          - allowed:     True if the request is within the limit
          - retry_after: seconds until the oldest entry in the window expires
                         (only meaningful when allowed is False)
        """
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            timestamps = self._requests.get(ip)
            if timestamps is None:
                timestamps = []
                self._requests[ip] = timestamps

            # Prune expired timestamps
            while timestamps and timestamps[0] <= cutoff:
                timestamps.pop(0)

            if len(timestamps) >= self._max_requests:
                # Denied – compute how long until the oldest entry expires
                retry_after = int(timestamps[0] - cutoff) + 1
                return False, retry_after

            # Allowed – record this request
            timestamps.append(now)
            return True, 0

    # -- background cleanup ------------------------------------------------

    def cleanup(self) -> int:
        """Remove tracking entries that have no timestamps in the current window. Returns the count of removed entries."""
        cutoff = time.monotonic() - self._window
        removed = 0
        with self._lock:
            stale = [ip for ip, ts in self._requests.items()
                     if not ts or ts[-1] <= cutoff]
            for ip in stale:
                del self._requests[ip]
            removed = len(stale)
        return removed


# ---------------------------------------------------------------------------
# Manager – orchestrates limiters, whitelist, cleanup thread
# ---------------------------------------------------------------------------

class _RateLimitManager:
    """Read config, build limiters, expose a single `check()` entry point."""

    def __init__(self, rate_cfg: dict) -> None:
        self.enabled = bool(rate_cfg.get('enabled', False))
        if not self.enabled:
            self._whitelist: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
            self._limiters: dict[str, _RateLimiter] = {}
            self._eviction_interval = 60
            return

        # Parse IP whitelist (individual IPs become /32 or /128)
        self._whitelist = []
        for entry in rate_cfg.get('ip_whitelist', []):
            try:
                self._whitelist.append(ipaddress.ip_network(str(entry), strict=False))
            except ValueError:
                log.warning('Ignoring invalid whitelist entry: %r', entry)

        # Build a limiter for each configured endpoint type
        self._limiters = {}
        for key in ('avatars', 'metadata'):
            section = rate_cfg.get(key, {})
            if not section.get('enabled', True):
                log.info('Rate limiting for %r is disabled.', key)
                continue
            requests = int(section.get('requests', 100))
            window = int(section.get('window', 60))
            self._limiters[key] = _RateLimiter(requests, window)
            log.info('Rate limiter [%s]: %d requests / %d s.', key, requests, window)

        self._eviction_interval = int(rate_cfg.get('eviction_interval', 60))

        if self._whitelist:
            log.info('Rate limiting whitelist: %s', ', '.join(str(n) for n in self._whitelist))

    # -- public API --------------------------------------------------------

    def check(self, endpoint_type: str, ip: str) -> Response | None:
        """Return None if the request is allowed, or a 429 Response if denied."""
        limiter = self._limiters.get(endpoint_type)
        if limiter is None:
            return None  # endpoint type not rate-limited

        if self._is_whitelisted(ip):
            return None

        allowed, retry_after = limiter.check(ip)
        if allowed:
            return None

        log.warning('Rate limit exceeded: %s from %s (endpoint=%s, retry_after=%ds).',
                    request.path, ip, endpoint_type, retry_after)
        body = json.dumps({'error': 'Too Many Requests', 'retry_after': retry_after})
        return Response(
            body,
            status=429,
            mimetype='application/json',
            headers={'Retry-After': str(retry_after)},
        )

    # -- whitelist ---------------------------------------------------------

    def _is_whitelisted(self, ip: str) -> bool:
        if not self._whitelist:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in network for network in self._whitelist)

    # -- background cleanup thread -----------------------------------------

    def start_cleanup_thread(self) -> None:
        if not self._limiters:
            return
        t = threading.Thread(target=self._cleanup_loop, name='ratelimit-cleanup', daemon=True)
        t.start()
        log.debug('Rate limit eviction thread started (interval=%ds).', self._eviction_interval)

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._eviction_interval)
            try:
                for name, limiter in self._limiters.items():
                    removed = limiter.cleanup()
                    if removed:
                        log.debug('Rate limiter [%s]: cleaned up %d stale entries.', name, removed)
            except Exception:
                log.exception('Rate limit cleanup iteration failed.')


# ---------------------------------------------------------------------------
# Module-level singleton (created once at import time / --preload)
# ---------------------------------------------------------------------------

_rate_cfg = cfg.get('rate_limiting', {})
_manager = _RateLimitManager(_rate_cfg)


def init_rate_limiting(app: Flask) -> None:
    """Register the before_request hook and start the cleanup thread."""
    if not _manager.enabled:
        log.info('Rate limiting is disabled.')
        return

    @app.before_request
    def _check_rate_limit():
        # Determine which endpoint type this request belongs to.
        # Only avatar and metadata serving are rate-limited.
        path = request.path

        if not path.startswith('/user-avatars/'):
            return None

        # Metadata paths contain /_metadata/ — check before the general avatar match
        if '/_metadata/' in path:
            endpoint_type = 'metadata'
        else:
            endpoint_type = 'avatars'

        return _manager.check(endpoint_type, request.remote_addr)

    _manager.start_cleanup_thread()
    log.info('Rate limiting registered on avatar and metadata endpoints.')
