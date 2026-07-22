# Akvatar - Distroless container image
#
# Multi-stage build:
#   1. Builder stage: installs Python dependencies into the system path
#   2. Final stage:   Google distroless Python image with only the app + deps
#
# Security:
#   - Runs as non-root (UID 65532, distroless "nonroot" user)
#   - Designed for read-only root filesystem (only volumes are writable)
#   - No shell, no package manager - minimal attack surface
#   - No .dockerignore needed - only explicitly listed files are copied

# ---------- Stage 1: build dependencies in a full Python image ----------
FROM python:3.13-slim-trixie@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

WORKDIR /build

# Install OS-level libs required to compile Pillow (JPEG, PNG, WebP, zlib, etc)
# and build wheels, then clean up in the same layer.
# https://pillow.readthedocs.io/en/stable/installation/building-from-source.html
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg62-turbo-dev \
        libpng-dev \
        libwebp-dev \
        libavif-dev \
        libtiff-dev \
        zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy uv binary from the official image. Pin to a specific tag or SHA for
# reproducible builds (consistent with the rest of this Dockerfile).
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

# Install Python dependencies into a staging directory (no venv needed in
# Docker). The target path is version-independent so the Dockerfile does
# not need updating when the base Python version changes.
#
# uv export reads the pre-resolved uv.lock and emits a pip-compatible
# requirements file with pinned hashes. uv pip install then fetches and
# installs exactly those versions - no dependency resolution, no dummy
# package build. --no-emit-project excludes the application itself (it is
# not an importable package; only its declared dependencies are needed).
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    uv pip install \
        --no-cache \
        --system \
        --target /opt/site-packages \
        -r /tmp/requirements.txt

# Resolve symlink chains for Pillow's shared libraries into a flat staging
# directory. Kaniko cannot follow multi-level .so symlinks across stages,
# so cp -L dereferences them to plain files before the COPY.
RUN mkdir -p /lib-staging && \
    cp -L \
        /usr/lib/x86_64-linux-gnu/libjpeg*.so* \
        /usr/lib/x86_64-linux-gnu/libpng*.so* \
        /usr/lib/x86_64-linux-gnu/libwebp*.so* \
        /usr/lib/x86_64-linux-gnu/libsharpyuv*.so* \
        /usr/lib/x86_64-linux-gnu/libz*.so* \
        /lib-staging/

# Create directory skeletons owned by nonroot (UID 65532) so Docker
# initialises named volumes with correct ownership on first run.
RUN mkdir -p /data-skel/user-avatars && \
    mkdir -p /config-skel && \
    chown -R 65532:65532 /data-skel /config-skel

# ---------- Stage 2: distroless runtime image ----------
# gcr.io/distroless/python3 contains only the Python interpreter and its
# core C libraries - no shell, no package manager, minimal attack surface.
# The :nonroot tag sets the default user to 65532 (nonroot).
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:828da6b298ecebf90580c84476c29b847b6432b46dbfaa642726b87ac527ee22

# Base version and Git commit short hash passed at build time via --build-arg.
# BASE_VERSION is parsed from src/__init__.py by the CI / build scripts so
# __init__.py stays the single source of truth for the version number.
# Both default to safe placeholders for plain `docker build` without explicit args.
ARG GIT_HASH=unknown
ARG BASE_VERSION=0.0.0
LABEL org.opencontainers.image.version="${BASE_VERSION}+${GIT_HASH}"

WORKDIR /app

# Copy installed packages into the version-independent dist-packages
# directory that is on every Debian Python's default sys.path.
COPY --from=builder /opt/site-packages /usr/lib/python3/dist-packages

# Copy shared libraries that Pillow needs at runtime (symlinks resolved in builder)
COPY --from=builder /lib-staging/ /usr/lib/x86_64-linux-gnu/

ENV PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    CONFIG_PATH="/data/config/config.yml" \
    APP_GIT_HASH="${GIT_HASH}"

# Copy application code and healthcheck binary (explicit files only)
COPY app.py run_app.py run_cleanup.py ./
COPY src/ src/
COPY static/ static/
COPY --from=ghcr.io/tarampampam/microcheck:1.4.0@sha256:c9f79cd408626de7c10f2d487d67339f49adf0ba61dde96ede65343269db1f85 /bin/httpscheck /bin/httpscheck

# Data directories - ownership inherited from builder skeletons so the
# nonroot user can write when Docker initialises the volumes.
COPY --from=builder --chown=65532:65532 /data-skel/ /data/
COPY --from=builder --chown=65532:65532 /config-skel/ /data/config/

VOLUME ["/data/user-avatars", "/data/config"]
HEALTHCHECK --interval=60s --timeout=3s --start-period=10s CMD ["/bin/httpscheck", "127.0.0.1:5000/healthz"]
EXPOSE 5000

# Launch via run_app.py which reads config.yml and starts gunicorn with --preload.
# A Python script is used because distroless images have no shell.
ENTRYPOINT ["python", "run_app.py"]
