"""
conftest.py - Test-session bootstrap and shared fixtures.

``src/config.py`` loads and validates ``config.yml`` at import time and exits
the process on any problem, and ``src/imaging.py`` freezes AVATAR_ROOT from that
config at import time too.  Both happen the moment anything under ``src`` is
imported, so the very first thing this file does - before any application
import - is write a throwaway config into a temp directory and point CONFIG_PATH
at it.  Everything below that line can then import ``src`` normally.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.config_fixture import base_config, write_config

# ---------------------------------------------------------------------------
# Session bootstrap - MUST run before the first `src` import
# ---------------------------------------------------------------------------

# One temp tree per pytest process: config.yml plus the avatar storage root that
# imaging.py resolves into AVATAR_ROOT / METADATA_ROOT.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="akvatar-tests-"))
_STORAGE_ROOT = _TEST_ROOT / "user-avatars"
_CONFIG_PATH = _TEST_ROOT / "config" / "config.yml"

write_config(_CONFIG_PATH, base_config(_STORAGE_ROOT))
os.environ["CONFIG_PATH"] = str(_CONFIG_PATH)
# Make sure a developer's DEBUG_MODE=true in the shell cannot flip the app into
# Flask debug mode and change the behavior under test.
os.environ.pop("DEBUG_MODE", None)
# Pin the version suffix so tests can assert on APP_VERSION deterministically.
os.environ.setdefault("APP_GIT_HASH", "testsha")

atexit.register(shutil.rmtree, _TEST_ROOT, True)

# Application imports are safe from here on.
from src.imaging import (  # noqa: E402  - deliberately after the bootstrap above
    AVATAR_ROOT,
    METADATA_ROOT,
    ensure_size_directories_existence,
)

# ---------------------------------------------------------------------------
# Session-wide paths
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_root() -> Path:
    """Root of the throwaway tree holding config.yml and the avatar storage."""
    return _TEST_ROOT


@pytest.fixture(scope="session")
def config_path() -> Path:
    """Path of the generated config.yml the whole session runs against."""
    return _CONFIG_PATH


@pytest.fixture(scope="session")
def avatar_root() -> Path:
    """The configured avatar storage root (imaging.AVATAR_ROOT)."""
    return AVATAR_ROOT


@pytest.fixture(scope="session")
def metadata_root() -> Path:
    """The metadata sidecar directory (imaging.METADATA_ROOT)."""
    return METADATA_ROOT


# ---------------------------------------------------------------------------
# Per-test isolation
# ---------------------------------------------------------------------------


def _wipe(directory: Path) -> None:
    """Delete every entry under *directory* without removing the directory itself."""
    if not directory.exists():
        return
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def clean_avatar_storage():
    """Give every test an empty, freshly-created avatar storage tree.

    AVATAR_ROOT is frozen at import time and shared by every module that touches
    the filesystem (imaging, cleanup, serve routes, the lock files in
    avatar_pipeline), so isolating by wiping the shared root is both simpler and
    more faithful than monkeypatching the constant in each module.
    """
    _wipe(AVATAR_ROOT)
    ensure_size_directories_existence()
    yield
    _wipe(AVATAR_ROOT)


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    """Replace every module-level requests.Session with a failing fake.

    Any outbound call a test did not explicitly stub raises AssertionError
    instead of reaching the network, so a missing stub surfaces as a clear test
    failure rather than a timeout or a real request.
    """
    from tests.helpers import FakeSession

    for module_path in ("src.authentik", "src.image_import", "src.webhooks"):
        module = __import__(module_path, fromlist=["_session"])
        monkeypatch.setattr(module, "_session", FakeSession(), raising=True)


# ---------------------------------------------------------------------------
# HTTP stubs
# ---------------------------------------------------------------------------


@pytest.fixture
def authentik_session(monkeypatch):
    """Install a scriptable fake session on ``src.authentik``.

    Returns the :class:`FakeSession`; assign ``.handler`` to script responses
    and read ``.calls`` to assert on what was sent.
    """
    import src.authentik as authentik
    from tests.helpers import FakeSession

    session = FakeSession()
    monkeypatch.setattr(authentik, "_session", session)
    # Retries sleep between attempts; keep the suite fast and deterministic.
    monkeypatch.setattr(authentik.time, "sleep", lambda _seconds: None)
    return session


@pytest.fixture
def import_session(monkeypatch):
    """Install a scriptable fake session on ``src.image_import``."""
    import src.image_import as image_import
    from tests.helpers import FakeSession

    session = FakeSession()
    monkeypatch.setattr(image_import, "_session", session)
    return session


@pytest.fixture
def webhook_session(monkeypatch):
    """Install a scriptable fake session on ``src.webhooks``."""
    import src.webhooks as webhooks
    from tests.helpers import FakeResponse, FakeSession

    session = FakeSession(handler=lambda method, url, **kw: FakeResponse(url=url))
    monkeypatch.setattr(webhooks, "_session", session)
    return session


@pytest.fixture
def allow_any_host(monkeypatch):
    """Treat every hostname as globally routable for SSRF checks.

    Import tests use ``.invalid`` and ``.example.com`` hostnames that do not
    resolve; without this the SSRF guard fails closed and blocks them before the
    code under test is reached.
    """
    import src.image_import as image_import

    monkeypatch.setattr(image_import, "resolves_to_private_ip", lambda _host: False)


# ---------------------------------------------------------------------------
# Flask application and clients
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def flask_app():
    """The application instance, built once for the whole session."""
    from app import create_app

    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(flask_app):
    """An unauthenticated Flask test client."""
    return flask_app.test_client()


# The session user dict every authenticated test starts from.  Mirrors what
# auth.process_oidc_callback() stores after a successful OIDC login.
TEST_USER = {
    "pk": 42,
    "username": "testuser",
    "name": "Test User",
    "email": "test.user@example.com",
    "avatar": "",
}


@pytest.fixture
def user() -> dict:
    """A copy of the canonical session user, safe for a test to mutate."""
    return dict(TEST_USER)


def login(test_client, user_dict: dict | None = None) -> str:
    """Populate a signed-in session on *test_client* and return its CSRF token."""
    import secrets

    token = secrets.token_hex(32)
    with test_client.session_transaction() as sess:
        sess["user"] = dict(user_dict or TEST_USER)
        sess["locale"] = "en_US"
        sess["csrf_token"] = token
    return token


@pytest.fixture
def authed_client(flask_app, user):
    """A test client with an authenticated session and a known CSRF token.

    The token is exposed as ``client.csrf_token`` so tests can send it in the
    ``X-CSRF-Token`` header without reaching into the session again.
    """
    test_client = flask_app.test_client()
    test_client.csrf_token = login(test_client, user)
    return test_client


@pytest.fixture
def csrf_headers(authed_client) -> dict:
    """Headers carrying a valid CSRF token for the authenticated client."""
    return {"X-CSRF-Token": authed_client.csrf_token}
