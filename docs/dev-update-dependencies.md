# Updating Dependencies

`pyproject.toml` pins each package to an exact version (e.g. `Flask==3.1.3`) so builds are
reproducible. Bump versions deliberately — Renovate opens PRs automatically for patch and
minor updates, so manual bumps are mostly for urgent fixes or major version jumps.

## Patch / minor bumps (via Renovate)

Renovate monitors `pyproject.toml` and opens update PRs automatically. Review and merge
them like any other dependency PR.

## Manual bumps

1. Check what's available:

   ```sh
   pip list --outdated

   # Or query a specific package
   pip index versions flask
   ```

2. Edit `pyproject.toml` — bump the version pin in the `dependencies` list, e.g.:

   ```toml
   "Flask==3.2.0",
   ```

3. Install the updated pins into your local environment:

   ```sh
   pip install --upgrade .
   ```

### Verify after any bump

```sh
python -m ruff check *.py src/
python run_cleanup.py          # smoke-test
```

If the app runs clean, commit `pyproject.toml`.

## CI / Dockerfile pins

The `Dockerfile` base images (`python:3.13-slim-trixie`, `gcr.io/distroless/python3-debian13`)
and the Kaniko image in `.gitlab-ci.yml` are managed by **Renovate** — open PRs are created
automatically. Review and merge them like any other dependency update.

The `microcheck` binary in the Dockerfile is also pinned by digest — Renovate handles it.
