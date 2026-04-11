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

Testing should primarily be done **inside the container** with debug mode enabled. This ensures the environment matches
production (distroless base image, non-root user, read-only filesystem).

## Submitting changes

When submitting a pull request:

1. **Explain why** the change is needed and **what** it does in the PR description
2. **Add screenshots** for any UI or visual changes
3. **Update documentation** in `docs/` if your change affects configuration, setup steps, or behavior described there
4. **Comment your code** with descriptive comments that explain what each block does (not just non-obvious logic, but
   all meaningful blocks)
5. Keep commits focused and messages clear

## Relevant resources

- [Configuration reference](docs/configuration.md) - all `config.yml` settings
- [How it works](docs/how-it-works.md) – application architecture and request flow
- [Authentik OIDC Setup](docs/authentik-oidc-setup.md) / [API Token](docs/authentik-api-token.md) - Authentik
  integration guide
- [Nginx Reverse Proxy](docs/nginx-reverse-proxy.md) / [Subfolder Deployment](docs/subfolder-deployment.md) - deployment
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

All four must change together. Renovate is intentionally prevented from bumping the Python minor version automatically —
the upgrade is a deliberate, coordinated change.

## License

By contributing, you agree that your contributions will be licensed under the [GPLv3 License](LICENSE).
