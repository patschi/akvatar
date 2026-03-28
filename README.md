# Authentik Avatar Updater

A self-hosted web application that lets users update their profile picture via a modern browser UI. The image is cropped client-side, processed into multiple sizes and formats on server-side, and then respective URLs pushed to **Authentik** (via API) and optionally to an **LDAP Server** (only tested Microsoft Active Directory, but any standards-compliant LDAP server should work).

## Features

- **OpenID Connect login** via Authentik (scopes: `openid profile email`)
- **Multiple Languages**: Automatically retrieved through OIDC `locale` attribute in `profile` scope. Currently supported English (default), German.
- **Client-side cropping** with [Cropper.js](https://github.com/fengyuanchen/cropperjs) (bundled locally, no external CDN)
- **Multi-size output**: 1024, 648, 512, 256, 128, 64 (configurable)
- **Multi-format output**: JPEG, PNG, WebP (configurable)
- **Unguessable filenames** (`uuid4` + `token_urlsafe` + nanosecond timestamp)
- **Authentik API**: sets `attributes.avatar-url` on the user object (configurable)
- **LDAP Server**: writes `thumbnailPhoto` (optional, toggle in config)
- **JSON metadata**: saves upload metadata (username, timestamp, sizes) per avatar
- **Configurable branding**: customise the application name in the UI
- **Reverse proxy / subfolder support**: respects `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-Prefix`
- **Optional TLS**: serve HTTPS directly from the built-in server
- **Distroless Docker image**: for minimal attack surface

## Quick start

The recommended way to run the application is via the **container image** (see [Running with Docker](#running-with-docker) below). For manual installation, see [Manual setup (Python)](#manual-setup-python).

### Container (recommended)

1. Create a `config.yml` from the example: `cp config.example.yml config.yml`
2. Fill in the required settings (see [Configuration](docs/configuration.md), [Authentik OIDC Setup](docs/authentik-oidc-setup.md), [Authentik API Token](docs/authentik-api-token.md), and [Flask Session Key](docs/flask-session-key.md))
3. Set up and run the container as seen at [Running with Docker](#running-with-docker) below

### Manual setup (Python)

1. Clone and install:

   ```bash
   git clone <repo-url> authentik-avatar-updater && cd authentik-avatar-updater
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Configure:

   ```bash
   cp data/config/config.example.yml data/config/config.yml
   ```

   Fill in the required settings (see [Configuration](docs/configuration.md), [Flask Session Key](docs/flask-session-key.md))

3. Run:

   ```bash
   python run.py
   ```

## Prerequisites

- An **Authentik** instance with an OIDC provider and API token (see [Authentik OIDC Setup](docs/authentik-oidc-setup.md) and [Authentik API Token](docs/authentik-api-token.md))
- *(Optional)* An LDAP server reachable via LDAPS/LDAP (tested with Microsoft Active Directory; see [MS AD Service Account](docs/ms-ad-service-account.md))
- **For container deployment**: Docker or any OCI-compatible runtime
- **For manual deployment**: Python 3.11+, Linux (Debian, Ubuntu, RHEL, Alpine, etc.)

## Running with Docker

### `docker run`

```bash
docker run -d \
  --name ak-avatar-updater \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp \
  -p 5000:5000 \
  -v ak-avatar-config:/app/data/config:ro \
  -v ak-avatar-data:/app/data/user-avatars \
  ghcr.io/patschi/ak-avatar-updater:latest
```

- The container runs as non-root (UID 65532) with a read-only root filesystem
- `/tmp` is mounted as tmpfs for gunicorn worker temp files
- Mount a volume at `/app/data/config` with a read-only bind for the configuration file (`config.yml`)
- Mount a volume at `/app/data/user-avatars` for persistent avatar storage

### Docker Compose

```yml
services:
  ak-avatar-updater:
    image: ghcr.io/patschi/ak-avatar-updater:latest
    container_name: ak-avatar-updater
    restart: unless-stopped
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges
    tmpfs:
      - /tmp
    ports:
      - "5000:5000"
    volumes:
      - ak-avatar-config:/app/data/config:ro
      - ak-avatar-data:/app/data/user-avatars

volumes:
  ak-avatar-data:
  ak-avatar-config:
```

Start with:

```bash
docker compose up -d
```

## Reverse proxy / subfolder deployment

The app fully supports running behind a reverse proxy (nginx, Caddy, Traefik, etc.) and under a subfolder path (e.g. `https://example.com/avatar/`). It honours `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix` headers automatically.

See the detailed guides:

- **[Nginx Reverse Proxy](docs/nginx-reverse-proxy.md)** -- full nginx configuration with TLS termination, SSE support, and optional direct avatar file serving
- **[Subfolder Deployment](docs/subfolder-deployment.md)** -- hosting the app under a URL path prefix (e.g. `/avatar/`)

## How it works

1. User visits the app and clicks **Sign in**
2. OIDC redirect -> Authentik login -> callback stores user info in session
3. Dashboard shows the user's current name and profile picture
4. User picks an image -> Cropper.js enforces a square crop in the browser
5. Cropped image is uploaded to `POST /api/upload`
6. Server validates, strips metadata, resizes to all configured sizes, and saves as JPG + PNG + WebP
7. Server PATCHes `attributes.avatar-url` on the Authentik user via API
8. *(If LDAP enabled)* Server writes the thumbnail JPEG into the configured LDAP photo attribute
9. Browser shows step-by-step progress via Server-Sent Events with success/fail status

For a detailed walkthrough with sequence diagrams, see **[How It Works](docs/how-it-works.md)**.

## Documentation

Extended guides are available in the [`docs/`](docs/) folder:

| Guide | Description |
|---|---|
| [Configuration](docs/configuration.md) | Complete reference for all `config.yml` settings with defaults and detailed explanations |
| [Flask Session Key](docs/flask-session-key.md) | Generating and setting the Flask session secret key |
| [Authentik OIDC Setup](docs/authentik-oidc-setup.md) | Creating the OIDC provider and application in Authentik |
| [Authentik API Token](docs/authentik-api-token.md) | Creating an API token for the Authentik Admin API |
| [TLS](docs/tls.md) | TLS certificate configuration and reverse proxy recommendation |
| [How It Works](docs/how-it-works.md) | Detailed procedure walkthrough with sequence diagrams |
| [Nginx Reverse Proxy](docs/nginx-reverse-proxy.md) | Full nginx configuration with TLS termination and SSE support |
| [Subfolder Deployment](docs/subfolder-deployment.md) | Hosting the app under a URL path prefix (e.g. `/avatar/`) |
| [MS AD Service Account](docs/ms-ad-service-account.md) | Least-privilege Active Directory service account setup with PowerShell automation |
