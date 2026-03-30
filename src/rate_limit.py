"""
rate_limit.py – Application-wide, thread-safe, sliding-window rate limiter.

Tracks request timestamps per client IP using shared state across all
gunicorn worker processes (via multiprocessing.Manager) and returns HTTP 429
when limits are exceeded.  Because state is shared, the effective limit per
client IP is exactly max_requests per window period.

Worker request-handling threads only read counts and append new timestamps
through the shared Manager proxy — they never prune or remove tracking
entries.  A single eviction thread in the master process periodically prunes
expired timestamps and removes empty entries, which is the only mechanism
that unblocks rate-limited IPs.

The eviction thread is started in init_rate_limiting(), which runs in the
master process when gunicorn uses --preload.  It operates on the same shared
Manager state that workers access, so eviction is truly application-wide.

With gunicorn gthread workers, each request-handling thread establishes its
own connection to the Manager server process (via thread-local storage), so
forked workers do not share connections with the master.
"""

import ipaddress
import json
import logging
import multiprocessing
import threading
import time

from flask import Flask, Response, request

from src.config import cfg

log = logging.getLogger('ratelimit')


# ---------------------------------------------------------------------------
# Sliding-window counter for a single endpoint type
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Track per-IP request timestamps with a fixed-size sliding window in shared state."""

    def __init__(self, name: str, max_requests: int, window: int,
                 eviction_interval: int, shared_dict, shared_lock) -> None:
        self._name = name
        self._max_requests = max_requests
        self._window = window
        self._eviction_interval = eviction_interval
        # Shared across all worker processes via multiprocessing.Manager
        self._requests = shared_dict   # {ip: [monotonic_timestamp, ...]}
        self._lock = shared_lock       # Manager Lock (cross-process)

    # -- hot path (read + append only, never prune) -------------------------

    def check(self, ip: str) -> tuple[bool, int]:
        """
        Record a request from *ip* and test the limit.

        Only counts and appends — expired timestamps are never removed here.
        Unblocking is handled exclusively by the eviction thread in the
        master process.

        Returns (allowed, retry_after):
          - allowed:     True if the request is within the limit
          - retry_after: seconds until the eviction thread is expected to
                         unblock this IP (only meaningful when allowed is False)
        """
        now = time.monotonic()

        with self._lock:
            timestamps = self._requests.get(ip, [])

            if len(timestamps) >= self._max_requests:
                # Denied — estimate when eviction will free enough slots.
                # The oldest timestamp must first expire (age past the window),
                # then the eviction thread must run to actually prune it.
                expires_at = timestamps[0] + self._window
                retry_after = max(1, int(expires_at - now) + 1 + self._eviction_interval)
                log.debug('[%s]: denied %s (%d/%d in list, retry_after=%ds).',
                          self._name, ip, len(timestamps), self._max_requests, retry_after)
                return False, retry_after

            # Allowed — record this request
            timestamps.append(now)
            self._requests[ip] = timestamps   # write back through Manager proxy
            current = len(timestamps)

        # Log at each 10% boundary of the limit (10%, 20%, … 90%)
        prev_tier    = ((current - 1) * 10) // self._max_requests
        current_tier = (current * 10) // self._max_requests
        if current_tier > prev_tier and current < self._max_requests:
            pct = (current * 100) // self._max_requests
            log.debug('[%s]: %s at %d%% of limit (%d/%d in window).',
                      self._name, ip, pct, current, self._max_requests)
        return True, 0

    # -- eviction (called only by the master process eviction thread) -------

    def evict(self) -> tuple[int, int]:
        """
        Prune expired timestamps from all tracked IPs and remove empty entries.

        Called exclusively by the eviction thread in the master process.
        Workers never call this method.

        Returns (pruned_total, evicted_ips): total timestamps pruned across
        all IPs, and number of IP entries fully removed.
        """
        cutoff = time.monotonic() - self._window
        pruned_total = 0
        evicted_ips = []

        with self._lock:
            for ip in list(self._requests.keys()):
                ts = self._requests[ip]
                before = len(ts)
                while ts and ts[0] <= cutoff:
                    ts.pop(0)
                pruned = before - len(ts)
                if pruned > 0:
                    pruned_total += pruned
                    if ts:
                        self._requests[ip] = ts   # write back pruned list
                        log.debug('[%s]: pruned %d expired timestamp(s) for %s, %d remain.',
                                  self._name, pruned, ip, len(ts))
                    else:
                        del self._requests[ip]
                        log.debug('[%s]: evicted %s (all %d timestamps expired).',
                                  self._name, ip, pruned)
                        evicted_ips.append(ip)
                elif not ts:
                    # Empty entry with nothing to prune — clean up
                    del self._requests[ip]
                    evicted_ips.append(ip)
            remaining = len(self._requests)

        if pruned_total or evicted_ips:
            log.debug('[%s]: eviction pass — pruned %d timestamp(s), evicted %d IP(s), %d active.',
                      self._name, pruned_total, len(evicted_ips), remaining)
        else:
            log.debug('[%s]: eviction pass — nothing to evict (%d active).',
                      self._name, remaining)

        return pruned_total, len(evicted_ips)


# ---------------------------------------------------------------------------
# Manager – orchestrates limiters, whitelist, eviction thread
# ---------------------------------------------------------------------------

class _RateLimitManager:
    """Read config, build limiters with shared state, expose a single `check()` entry point."""

    def __init__(self, rate_cfg: dict) -> None:
        self.enabled = bool(rate_cfg.get('enabled', False))
        if not self.enabled:
            self._whitelist: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
            self._limiters: dict[str, _RateLimiter] = {}
            self._eviction_interval = 60
            return

        # Shared-state server process for cross-worker data.
        # multiprocessing.Manager starts a server process that holds the actual
        # data; workers and the master access it via proxy objects over IPC.
        self._mp_manager = multiprocessing.Manager()

        # Parse IP whitelist (individual IPs become /32 or /128)
        self._whitelist = []
        for entry in rate_cfg.get('ip_whitelist', []):
            try:
                self._whitelist.append(ipaddress.ip_network(str(entry), strict=False))
            except ValueError:
                log.warning('Ignoring invalid whitelist entry: %r', entry)

        self._eviction_interval = int(rate_cfg.get('eviction_interval', 60))

        # Build a limiter for each configured endpoint type, each with its own
        # shared dict and lock managed by the Manager server process.
        self._limiters = {}
        for key in ('avatars', 'metadata'):
            section = rate_cfg.get(key, {})
            if not section.get('enabled', True):
                log.info('Rate limiting for %r is disabled.', key)
                continue
            requests = int(section.get('requests', 100))
            window = int(section.get('window', 60))
            shared_dict = self._mp_manager.dict()
            shared_lock = self._mp_manager.Lock()
            self._limiters[key] = _RateLimiter(
                key, requests, window, self._eviction_interval,
                shared_dict, shared_lock,
            )
            log.info('Rate limiter [%s]: %d requests / %d s window.', key, requests, window)

        log.info('Rate limiting eviction interval: %ds.', self._eviction_interval)

        if self._whitelist:
            log.info('Rate limiting whitelist: %s', ', '.join(str(n) for n in self._whitelist))
        else:
            log.debug('Rate limiting whitelist: empty (all IPs are subject to limits).')

    # -- public API --------------------------------------------------------

    def check(self, endpoint_type: str, ip: str) -> Response | None:
        """Return None if the request is allowed, or a 429 Response if denied."""
        limiter = self._limiters.get(endpoint_type)
        if limiter is None:
            log.debug('Rate limiting: endpoint type %r has no limiter – skipping.', endpoint_type)
            return None

        if self._is_whitelisted(ip):
            return None

        allowed, retry_after = limiter.check(ip)
        if allowed:
            return None

        log.warning('Rate limit exceeded [%s]: %s from %s (retry_after=%ds).',
                    endpoint_type, request.path, ip, retry_after)
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
            log.debug('Rate limiting: could not parse client IP %r – applying limits.', ip)
            return False
        result = any(addr in network for network in self._whitelist)
        if result:
            log.debug('Rate limiting: %s is whitelisted – skipping.', ip)
        return result

    # -- central eviction thread (master process only) ---------------------

    def start_eviction_thread(self) -> None:
        """Start the eviction thread once in the master process."""
        if not self._limiters:
            log.debug('Rate limiting: no active limiters, eviction thread not started.')
            return
        t = threading.Thread(target=self._eviction_loop, name='ratelimit-evict', daemon=True)
        t.start()
        log.debug('Rate limit eviction thread started (interval=%ds).', self._eviction_interval)

    def _eviction_loop(self) -> None:
        while True:
            time.sleep(self._eviction_interval)
            try:
                for limiter in self._limiters.values():
                    limiter.evict()
            except Exception:
                log.exception('Rate limit eviction iteration failed.')


# ---------------------------------------------------------------------------
# Module-level singleton (created once at import time / --preload)
# ---------------------------------------------------------------------------

_rate_cfg = cfg.get('rate_limiting', {})
_manager = _RateLimitManager(_rate_cfg)


def init_rate_limiting(app: Flask) -> None:
    """Register the before_request hook and start the central eviction thread."""
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

    # Start the eviction thread in the master process.
    # With gunicorn --preload, create_app() runs in the master before workers
    # are forked.  The eviction thread stays in the master and operates on
    # shared state via the multiprocessing.Manager server process.  Workers
    # never run eviction — they only read/append through Manager proxies.
    _manager.start_eviction_thread()

    log.info('Rate limiting registered on avatar and metadata endpoints.')
