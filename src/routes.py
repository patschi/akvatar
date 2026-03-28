"""
routes.py – Flask route definitions.

Contains:
  - GET  /              -> public login page (unauthenticated)
  - GET  /dashboard     -> avatar upload / crop page (authenticated)
  - GET  /user-avatars/<X>x<Y>/<file>  -> serve stored avatar images
  - GET  /user-avatars/_metadata/<file> -> serve avatar metadata JSON
  - POST /api/upload    -> accept cropped image, process, update backends
"""

import logging

from flask import (
    Blueprint, Response, redirect, url_for, session, request,
    jsonify, send_from_directory, render_template, stream_with_context,
)

from src.auth import login_required
from src.imaging import AVATAR_ROOT, MAX_SIZE, ALLOWED_EXTENSIONS
from src.ldap_client import is_enabled as ldap_is_enabled
from src.upload import validate_upload, generate_sse, ValidationError

log = logging.getLogger('routes')

routes_bp = Blueprint('routes', __name__)


# ---------------------------------------------------------------------------
# robots.txt – block all search engine crawling
# ---------------------------------------------------------------------------
@routes_bp.route('/robots.txt')
def robots_txt():
    """Serve robots.txt from the in-memory static cache."""
    from app import _static_cache
    entry = _static_cache.get('robots.txt')
    if entry is None:
        return Response('', status=404)
    data, mime, _ = entry
    return Response(data, mimetype=mime)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@routes_bp.route('/healthz')
def healthz():
    """Lightweight health probe for load balancers or healthchecks."""
    return Response('OK', mimetype='text/plain')


# ---------------------------------------------------------------------------
# Public login page
# ---------------------------------------------------------------------------
@routes_bp.route('/')
def login_page():
    """Show a static landing page with a login button, or redirect to dashboard if already authenticated."""
    if 'user' in session:
        log.debug('User already authenticated – redirecting to dashboard.')
        return redirect(url_for('routes.dashboard'))
    error_key = request.args.get('error', '')
    if not error_key and 'autologin' in request.args:
        return redirect(url_for('auth.login'))
    if error_key:
        log.debug('Login page rendered with error=%r.', error_key)
    return render_template('login.html', error_key=error_key)


# ---------------------------------------------------------------------------
# Dashboard (authenticated)
# ---------------------------------------------------------------------------
@routes_bp.route('/dashboard')
@login_required
def dashboard():
    """Serve the authenticated avatar upload / crop page."""
    user = session['user']
    log.debug('Serving dashboard for user %r.', user['username'])
    return render_template(
        'dashboard.html', user=user, ldap_enabled=ldap_is_enabled(),
        max_size=MAX_SIZE, allowed_extensions=sorted(ALLOWED_EXTENSIONS),
    )


# ---------------------------------------------------------------------------
# Serve stored avatar files
# ---------------------------------------------------------------------------
@routes_bp.route('/user-avatars/<dimensions>/<filename>')
def serve_avatar(dimensions, filename):
    """Serve avatar image files from the storage directory. `send_from_directory` prevents directory-traversal attacks."""
    filepath = f'{dimensions}/{filename}'
    log.debug('Serving avatar file: %s', filepath)
    return send_from_directory(AVATAR_ROOT, filepath)


# ---------------------------------------------------------------------------
# Serve avatar metadata files
# ---------------------------------------------------------------------------
@routes_bp.route('/user-avatars/_metadata/<filename>')
def serve_avatar_metadata(filename):
    """Serve avatar metadata JSON from the storage directory."""
    log.debug('Serving metadata file: _metadata/%s', filename)
    return send_from_directory(AVATAR_ROOT, f'_metadata/{filename}', mimetype='application/json')


# ---------------------------------------------------------------------------
# Upload & process API  (Server-Sent Events for real-time progress)
# ---------------------------------------------------------------------------
@routes_bp.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """
    Accept a cropped image blob, validate it synchronously, then stream
    processing progress back to the client as Server-Sent Events.

    Validation failures return a normal JSON 400 response.
    Once validation passes the response switches to ``text/event-stream``
    and each processing step is pushed as it completes.
    """
    user = session['user']
    log.info('Upload request from user %r.', user['username'])

    # -- Synchronous validation (returns JSON 400 on failure) ------------------
    if 'file' not in request.files:
        log.warning('Upload rejected – no file part in request.')
        return jsonify({'error': 'No file part in the request.'}), 400

    try:
        image = validate_upload(request.files['file'])
    except ValidationError as exc:
        log.warning('Upload rejected: %s', exc)
        return jsonify({'error': str(exc)}), 400

    log.info('Image validated – mode=%s, size=%dx%d. Starting SSE stream.',
             image.mode, image.width, image.height)

    # -- Stream processing progress as SSE -------------------------------------
    return Response(
        stream_with_context(generate_sse(user, image)),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
