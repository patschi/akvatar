"""
app_static.py – In-memory static file cache.

All files in static/ are read once at import time and served from memory.
With gunicorn --preload, workers inherit the cache via fork (shared pages).
"""

import hashlib
import logging
import mimetypes
from pathlib import Path

from flask import Response, abort, request

log = logging.getLogger('app.static')

# Resolve the static directory relative to this file's location (src/ → project root → static/)
_STATIC_DIR = Path(__file__).resolve().parent.parent / 'static'


def _build_static_cache() -> dict[str, tuple[bytes, str, str]]:
    """Read every file under static/ into {rel_path: (data, mimetype, etag)}."""
    cache = {}
    for path in sorted(_STATIC_DIR.rglob('*')):
        if not path.is_file():
            continue
        log.debug('Caching static file: %s', path)
        rel = path.relative_to(_STATIC_DIR).as_posix()
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        etag = hashlib.sha256(data).hexdigest()[:16]
        cache[rel] = (data, mime, etag)
    log.debug('Static file cache built with %d file(s).', len(cache))
    return cache


def serve_static_file(filename: str) -> Response:
    """Serve a file from the in-memory static cache with ETag/304 support."""
    entry = static_cache.get(filename)
    if entry is None:
        abort(404)
    data, mime, etag = entry
    headers = {'ETag': f'"{etag}"', 'Cache-Control': 'public, max-age=86400'}
    if request.if_none_match and etag in request.if_none_match:
        return Response(status=304, headers=headers)
    return Response(data, mimetype=mime, headers=headers)


static_cache = _build_static_cache()
log.info('Static file cache: %d file(s), %.1f KB total.',
         len(static_cache),
         sum(len(d) for d, _, _ in static_cache.values()) / 1024)
