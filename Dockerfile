# ============================================================================
# Authentik Avatar Updater – Distroless container image
#
# Multi-stage build:
#   1. Builder stage: installs Python dependencies into a virtual env
#   2. Final stage:   Google distroless Python image with only the app + venv
#
# No .dockerignore is used — only explicitly listed files are copied into the
# image, so the build context never leaks secrets, data, or dev artefacts.
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

# Create a virtual environment so we can copy it cleanly to the final stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies (separate layer — cached until requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Stage 2: distroless runtime image ----------
# gcr.io/distroless/python3 contains only the Python interpreter and its
# core C libraries — no shell, no package manager, minimal attack surface.
FROM gcr.io/distroless/python3-debian13

WORKDIR /app

# Copy the virtual env (with all installed packages) from the builder
COPY --from=builder /opt/venv /opt/venv

# Copy shared libraries that Pillow needs at runtime (single layer)
COPY --from=builder \
    /usr/lib/x86_64-linux-gnu/libjpeg*.so* \
    /usr/lib/x86_64-linux-gnu/libpng*.so* \
    /usr/lib/x86_64-linux-gnu/libwebp*.so* \
    /usr/lib/x86_64-linux-gnu/libsharpyuv*.so* \
    /usr/lib/x86_64-linux-gnu/libz*.so* \
    /usr/lib/x86_64-linux-gnu/

# Set virtualenv on the path so Python finds the installed packages
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

# Copy application code and healthcheck binary (explicit files only — no .dockerignore needed)
COPY app.py cleanup.py ./
COPY src/ src/
COPY static/ static/

COPY --from=ghcr.io/tarampampam/microcheck:1.3.0@sha256:79c187c05bfa67518078bf4db117771942fa8fe107dc79a905861c75ddf28dfa /bin/httpscheck /bin/httpscheck
HEALTHCHECK --interval=60s --timeout=3s CMD ["/bin/httpscheck", "localhost:5000/healthz"]

EXPOSE 5000
VOLUME ["/app/data/user-avatars", "/app/data/config"]

# Run the app via gunicorn for production use.
# Distroless images use the ENTRYPOINT array form (no shell).
ENTRYPOINT ["python", "-m", "gunicorn", \
            "--bind", "0.0.0.0:5000", \
            "--workers", "2", \
            "--access-logfile", "-", \
            "app:create_app()"]
