# Contributing

Thank you for your interest in contributing! Please read this guide before submitting changes.

## Reporting issues

Found a bug or have a feature request? [Open an issue](../../issues) first. Describe what you observed, what you
expected, and include screenshots if the issue is visual.

For **larger changes** (new features, refactors, architectural changes), always file an issue first to discuss the
approach before writing code. This avoids wasted effort if the direction needs adjustment.

## Development setup

### Prerequisites

- Docker (or any OCI-compatible runtime)
- A `data/config/config.yml` based on `data/config/config.example-minimal.yml` or
  `config.example-full.yml` (see [Configuration](docs/configuration.md))

### Running locally

Use the development compose file, which builds the container from source and enables debug mode (`DEBUG_MODE=true`) by
default:

```bash
docker compose -f compose.dev.yml up --build akvatar
```

Debug mode enables Flask's debugger, template auto-reload, and verbose `DEBUG`-level logging.

### Rebuilding from clean state

Docker layer caching can cause stale dependencies or code to persist. To ensure a fully clean build:

```bash
# Stop and remove the dev container and its volumes
docker compose -f compose.dev.yml down -v

# Rebuild without using cached layers
docker compose -f compose.dev.yml build --no-cache akvatar

# Start fresh
docker compose -f compose.dev.yml up akvatar
```

If you changed `pyproject.toml`, always rebuild with `--no-cache` (or at minimum `--build`) to pick up new
dependencies.

## Testing

### Automated test suite

The project ships a pytest suite under `tests/`. It runs entirely offline: every outbound `requests.Session`
(Authentik, Gravatar/URL import, webhooks) and every LDAP connection is replaced with a fake in
`tests/conftest.py`, so no test ever reaches the network or a real backend.

```bash
# Install the dev dependency group and run everything
uv sync --group dev
uv run pytest

# Fast inner loop - skip the subprocess-based config validation cases
uv run pytest -m "not slow"

# With a coverage summary
uv run pytest --cov --cov-report=term
```

**How the suite bootstraps.** `src/config.py` reads and validates `config.yml` at *import* time and calls
`sys.exit(1)` on any problem, and `src/imaging.py` freezes `AVATAR_ROOT` from that config at import time too.
`tests/conftest.py` therefore writes a throwaway config into a temp directory and points `CONFIG_PATH` at it
*before* the first `src` import. The configuration it uses lives in `tests/config_fixture.py`; changing a value
there (image sizes, LDAP photo entries, webhook endpoints) changes what the whole suite runs against.

**Writing a test.**

- Filesystem state is isolated by an autouse fixture that wipes and recreates the avatar storage tree around every
  test, so tests can write real image files and assert on them.
- Use the `client` / `authed_client` fixtures for HTTP-level tests; `authed_client.csrf_token` (or the
  `csrf_headers` fixture) carries a valid CSRF token.
- Use `authentik_session`, `import_session` and `webhook_session` to script outbound HTTP responses. An outbound
  call that no test stubbed raises `AssertionError` rather than silently reaching the network.
- Image factories (`tests/helpers.py`) build real encoder output - use `noisy_image()` when a test depends on file
  size, since solid colors compress to a few hundred bytes in every format.
- Config validation is tested by booting a fresh interpreter against a purpose-built config file
  (`tests/test_config_validation.py`); those cases are marked `slow`.

`tests/test_repo_consistency.py` holds the cross-file invariants: static assets referenced by templates must
exist, the Dockerfile must copy every entry point, and the CI pipeline must keep the test stage in front of the
container build.

It also enforces the translation catalog in both directions - **every key used must exist, and every key that
exists must be used**:

- A `t('some.key')` call in a template or in Python must resolve to a key in `src/languages/en_US.yml`.
- Every key in `en_US.yml` must be referenced somewhere: a literal `t()` call, the `login.error_*` lookup that
  `login.html` builds dynamically from `_VALID_ERROR_KEYS`, or `_JS_KEYS`. Adding a string you do not wire up
  yet will fail the suite - wire it up or leave it out.
- Every key in `_JS_KEYS` must actually be read as `I18N.<name>` by `static/js/*.js`, and vice versa, so the
  per-page translation payload stays exactly as large as the browser needs.
- Every locale must carry the full English key set in its own file (not via the startup backfill), and a
  translated string must use the same `{placeholders}` as the English one - a renamed placeholder is a runtime
  `KeyError` inside `t()`.

### Manual testing

Beyond the automated suite, changes should also be exercised **inside the container** with debug mode enabled.
This ensures the environment matches production (distroless base image, non-root user, read-only filesystem).

## Submitting changes

When submitting a pull request:

1. **Explain why** the change is needed and **what** it does in the PR description
2. **Run the test suite** (`uv run pytest`) and add or update tests covering your change
3. **Add screenshots** for any UI or visual changes (see [docs/screenshots.md](docs/screenshots.md) for the existing
   visual walkthrough)
4. **Update documentation** in `docs/` if your change affects configuration, setup steps, or behavior described there
5. **Comment your code** with descriptive comments that explain what each block does (not just non-obvious logic, but
   all meaningful blocks)
6. Keep commits focused and messages clear

## Relevant resources

- [Configuration reference](docs/configuration.md) - all `config.yml` settings
- [How it works](docs/how-it-works.md) - application architecture and request flow
- [Authentik OIDC Setup](docs/authentik-oidc-setup.md) / [API Token](docs/authentik-api-token.md) - Authentik
  integration guide
- [nginx Reverse Proxy](docs/nginx-reverse-proxy.md) / [Subfolder Deployment](docs/subfolder-deployment.md) - deployment
  guides
- [Cropper.js documentation](https://github.com/fengyuanchen/cropperjs) - client-side image cropping library

## Python version alignment

The builder stage in `Dockerfile` (`python:3.13-slim-trixie`) and the runtime stage (
`gcr.io/distroless/python3-debian13`) must always use the **same Python minor version**. They are intentionally tied to
the same Debian release (Trixie = Debian 13).

When bumping the Python version (e.g., moving to Python 3.14 with a new distroless base):

1. Update the `FROM python:3.13-slim-...` line in `Dockerfile` to the new version and Debian codename
2. Update the `FROM gcr.io/distroless/python3-debian13` line to the matching `python3-debian14` (or equivalent) tag
3. Update the `image: python:3.13-slim@sha256:...` line in `.gitlab-ci.yml` to the new version
4. Update the `allowedVersions` regex in `renovate.json` from `/^3\.13/` to `/^3\.14/`

All four must change together. Renovate is intentionally prevented from bumping the Python minor version automatically -
the upgrade is a deliberate, coordinated change.

## License

By contributing, you agree that your contributions will be licensed under the [GPLv3 License](LICENSE).
