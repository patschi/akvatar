# ============================================================================
# Authentik Avatar Updater – Distroless container image
#
# Uses a multi-stage build:
#   1. Builder stage: installs Python dependencies into a virtual env
#   2. Final stage:   Google distroless Python image with only the app + venv
# ============================================================================

# ---------- Stage 1: build dependencies in a full Python image ----------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install OS-level libs required to compile Pillow (JPEG, PNG, WebP, zlib)
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

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Stage 2: distroless runtime image ----------
# gcr.io/distroless/python3 contains only the Python interpreter and its
# core C libraries – no shell, no package manager, minimal attack surface.
FROM gcr.io/distroless/python3-debian12

# Copy the virtual env (with all installed packages) from the builder
COPY --from=builder /opt/venv /opt/venv

# Copy shared libraries that Pillow needs at runtime
COPY --from=builder /usr/lib/x86_64-linux-gnu/libjpeg*.so*    /usr/lib/x86_64-linux-gnu/
COPY --from=builder /usr/lib/x86_64-linux-gnu/libpng*.so*     /usr/lib/x86_64-linux-gnu/
COPY --from=builder /usr/lib/x86_64-linux-gnu/libwebp*.so*    /usr/lib/x86_64-linux-gnu/
COPY --from=builder /usr/lib/x86_64-linux-gnu/libsharpyuv*.so*  /usr/lib/x86_64-linux-gnu/
COPY --from=builder /usr/lib/x86_64-linux-gnu/libz*.so*       /usr/lib/x86_64-linux-gnu/

# Set virtualenv on the path so Python finds the installed packages
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"

WORKDIR /app

# Copy application code
COPY app.py .
COPY src/ src/
COPY templates/ templates/
COPY static/ static/

# Create the avatar storage directory (volume-mountable)
# Note: distroless has no shell, so we prepare this in the builder
COPY --from=builder /build /tmp/empty
VOLUME ["/app/data/user-avatars"]

# The config file is expected to be mounted at /app/data/config/config.yml
# (or set CONFIG_PATH env var to override)
ENV CONFIG_PATH="/app/data/config/config.yml"

EXPOSE 5000

# Run the app via gunicorn for production use.
# Distroless images use the ENTRYPOINT array form (no shell).
ENTRYPOINT ["python", "-m", "gunicorn", \
            "--bind", "0.0.0.0:5000", \
            "--workers", "2", \
            "--access-logfile", "-", \
            "app:create_app()"]
