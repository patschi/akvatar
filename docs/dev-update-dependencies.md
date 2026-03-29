# Updating Dependencies

`requirements.txt` pins each package to a minor version (e.g. `Flask==3.1.*`) so patch
releases are picked up automatically by pip, while major/minor bumps require a deliberate
edit.

## Patch updates (same minor pin)

No `requirements.txt` change needed — pip already resolves the latest patch.

```sh
pip install --upgrade -r requirements.txt
```

## Minor / major bumps

### Automatic (recommended)

[`pur`](https://github.com/alanhamlett/pip-update-requirements) rewrites version pins in
`requirements.txt` in-place to the latest available release while preserving the existing
constraint style (`==X.Y.*` stays `==X.Y.*`, just with a new version number).

```sh
# Install once
pip install pur

# Preview what would change without touching the file
pur -r requirements.txt --dry-run --dry-run-changed

# Write updates to requirements.txt (bumps patch + minor + major)
pur -r requirements.txt

# Install the updated pins
pip install --upgrade -r requirements.txt
```

> **Note:** `pur` bumps to the absolute latest, including major version jumps. Review the
> diff (`git diff requirements.txt`) and check changelogs for breaking changes before
> installing.

### Manual

1. Check what's available:

   ```sh
   pip list --outdated

   # Or query a specific package
   pip index versions flask
   ```

2. Edit `requirements.txt` — bump the minor (or major) version pin, e.g.:

   ```
   Flask==3.2.*
   ```

3. Install the new pins:

   ```sh
   pip install --upgrade -r requirements.txt
   ```

### Verify after any bump

```sh
python -m ruff check *.py src/
python run_cleanup.py          # smoke-test
```

If the app runs clean, commit `requirements.txt`.

## CI / Dockerfile pins

The `Dockerfile` base images (`python:3.13-slim-trixie`, `gcr.io/distroless/python3-debian13`)
and the Kaniko image in `.gitlab-ci.yml` are managed by **Renovate** — open PRs are created
automatically. Review and merge them like any other dependency update.

The `microcheck` binary in the Dockerfile is also pinned by digest — Renovate handles it.
