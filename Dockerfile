# ============================================================================
# Authentik Avatar Updater – Distroless container image
#
# Multi-stage build:
#   1. Builder stage: installs Python dependencies into the system path
#   2. Final stage:   Google distroless Python image with only the app + deps
#
# Security:
#   - Runs as non-root (UID 65532, distroless "nonroot" user)
#   - Designed for read-only root filesystem (only volumes are writable)
#   - No shell, no package manager — minimal attack surface
#   - No .dockerignore needed — only explicitly listed files are copied
# ============================================================================

# ---------- Stage 1: build dependencies in a full Python image ----------
FROM python:3.13-slim-trixie AS builder

WORKDIR /build

# Install OS-level libs required to compile Pillow (JPEG, PNG, WebP, zlib)
# and build wheels, then clean up in the same layer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg62-turbo-dev \
        libpng-dev \
        libwebp-dev \
        zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a staging directory (no venv needed in
# Docker). The target path is version-independent so the Dockerfile does
# not need updating when the base Python version changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --target /opt/site-packages -r requirements.txt

# Create data directory skeleton owned by nonroot (UID 65532) so Docker
# initialises named volumes with correct ownership on first run.
RUN mkdir -p /data-skel/user-avatars /data-skel/config && \
    chown -R 65532:65532 /data-skel

# ---------- Stage 2: distroless runtime image ----------
# gcr.io/distroless/python3 contains only the Python interpreter and its
# core C libraries — no shell, no package manager, minimal attack surface.
# The :nonroot tag sets the default user to 65532 (nonroot).
FROM gcr.io/distroless/python3-debian13:nonroot

WORKDIR /app

# Copy installed packages into the version-independent dist-packages
# directory that is on every Debian Python's default sys.path.
COPY --from=builder /opt/site-packages /usr/lib/python3/dist-packages

# Copy shared libraries that Pillow needs at runtime (single layer)
COPY --from=builder \
    /usr/lib/x86_64-linux-gnu/libjpeg*.so* \
    /usr/lib/x86_64-linux-gnu/libpng*.so* \
    /usr/lib/x86_64-linux-gnu/libwebp*.so* \
    /usr/lib/x86_64-linux-gnu/libsharpyuv*.so* \
    /usr/lib/x86_64-linux-gnu/libz*.so* \
    /usr/lib/x86_64-linux-gnu/

ENV PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

# Copy application code and healthcheck binary (explicit files only)
COPY app.py cleanup.py run.py ./
COPY src/ src/
COPY static/ static/
COPY --from=ghcr.io/tarampampam/microcheck:1.3.0@sha256:79c187c05bfa67518078bf4db117771942fa8fe107dc79a905861c75ddf28dfa /bin/httpscheck /bin/httpscheck

# Data directories — ownership inherited from builder skeleton so the
# nonroot user can write when Docker initialises the volumes.
COPY --from=builder --chown=65532:65532 /data-skel/ /app/data/

VOLUME ["/app/data/user-avatars", "/app/data/config"]
HEALTHCHECK --interval=60s --timeout=3s CMD ["/bin/httpscheck", "localhost:5000/healthz"]
EXPOSE 5000

# Launch via run.py which reads config.yml and starts gunicorn with --preload.
# A Python script is used because distroless images have no shell.
ENTRYPOINT ["python", "run.py"]
