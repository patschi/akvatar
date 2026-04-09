# Akvatar – Authentik Avatar Updater

<p align="center"><img src="static/images/favicon-192.png" alt="Akvatar logo" width="128" height="128"></p>

A self-hosted web application that lets users update their profile picture via a modern
browser UI. The image is cropped client-side, processed into multiple sizes and formats
server-side, then pushed to **Authentik** (via Admin API) and optionally to an
**LDAP / Active Directory** server.

## Features

- **OpenID Connect login** via Authentik (scopes: `openid profile email`)
- **Multi-language UI**: locale resolved from the OIDC `locale` claim; currently
  English (default) and German
- **Client-side square cropping** with [Cropper.js](https://github.com/fengyuanchen/cropperjs)
  (bundled locally, no external CDN)
- **Multi-size output**: configurable square sizes (see [Configuration](docs/configuration.md#images_sizes))
- **Multi-format output**: JPEG, PNG, WebP with configurable quality settings
  (see [Configuration](docs/configuration.md#images_formats))
- **Privacy-first image handling**: EXIF orientation applied to pixels then all metadata
  stripped (GPS, device info, ICC profiles, XMP, IPTC)
- **Unguessable filenames**: `uuid4` hex + `token_urlsafe(64)` + nanosecond timestamp
  (~740 bits of entropy)
- **Authentik Admin API**: sets a configurable user attribute (default: `avatar-url`) on
  the user object via `PATCH /api/v3/core/users/{pk}/`
- **LDAP / Active Directory**: writes one or more photo attributes (binary bytes or URL
  string); optional, toggle in config
- **Automatic cleanup**: cron-scheduled job removes avatars of deleted users, enforces
  per-user retention limits, and clears orphaned files from obsolete sizes or formats
- **Real-time progress**: Server-Sent Events stream each processing step with
  success / failed / skipped / dry-run status
- **Configurable branding**: customize the application name in the UI
- **Reverse proxy / subfolder support**: honours `X-Forwarded-For`, `X-Forwarded-Proto`,
  `X-Forwarded-Host`, `X-Forwarded-Prefix`
- **Optional built-in TLS**: serve HTTPS directly without a reverse proxy
- **Dry-run mode**: processes and saves images but skips all Authentik and LDAP writes;
  logs what would have happened instead
- **Rate limiting**: per-IP point budget on avatar and metadata endpoints, with CIDR
  whitelist support and a configurable 404 penalty
- **Security response headers**: `X-Content-Type-Options`, `X-Frame-Options` (HTML
  only), and `Referrer-Policy` set on every response
- **In-memory static file cache**: all static assets are read once at startup and served
  from RAM with ETag/304 support; no per-request disk I/O
- **Health check endpoint**: `GET /healthz` returns `200 OK` for load-balancer probes
- **Secure container image**: distroless base, non-root (UID 65532), read-only root
  filesystem, `cap_drop: ALL`

## Quick start

The recommended way to run the application is via the **container image**.
For manual installation see [Manual setup (Python)](#manual-setup-python).

### Container (recommended)

1. Copy the example config:

   ```bash
   # Minimal — required settings only (recommended starting point)
   cp data/config/config.example-minimal.yml data/config/config.yml

   # Full — every option with inline comments
   # cp data/config/config.example-full.yml data/config/config.yml
   ```

2. Fill in the required settings — see
   [Configuration](docs/configuration.md),
   [Authentik OIDC Setup](docs/authentik-oidc-setup.md),
   [Authentik API Token](docs/authentik-api-token.md), and
   [Flask Session Key](docs/flask-session-key.md).

3. Run with Docker Compose (see [Running with Docker](#running-with-docker) below).

### Manual setup (Python)

1. Clone and install:

   ```bash
   git clone https://github.com/patschi/akvatar.git akvatar && cd akvatar
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Configure:

   ```bash
   cp data/config/config.example-minimal.yml data/config/config.yml
   # Edit data/config/config.yml
   ```

3. Run:

   ```bash
   python run_app.py
   ```

## Prerequisites

- An **Authentik** instance with an OIDC provider and an Admin API token
  (see [Authentik OIDC Setup](docs/authentik-oidc-setup.md) and
  [Authentik API Token](docs/authentik-api-token.md))
- *(Optional)* An LDAP server reachable via LDAPS/LDAP — tested with Microsoft Active
  Directory; see [MS AD Service Account](docs/ms-ad-service-account.md)
- **Container deployment:** Docker or any OCI-compatible runtime
- **Manual deployment:** Python 3.11+, Linux (Debian, Ubuntu, RHEL, Alpine, etc.)

## Running with Docker

### `docker run`

```bash
docker run -d \
  --name akvatar \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp \
  -p 5000:5000 \
  -v akvatar-conf:/app/data/config:ro \
  -v akvatar-data:/app/data/user-avatars \
  ghcr.io/patschi/akvatar:latest
```

- Runs as non-root (UID 65532) with a read-only root filesystem
- `/tmp` is a tmpfs mount — required for gunicorn worker temp files
- `/app/data/config` — read-only volume containing `config.yml`
- `/app/data/user-avatars` — writable volume for persistent avatar storage

#### Bind-mount directories instead of named volumes

If you prefer host directories over named Docker volumes, create them with correct
ownership first (container runs as UID 65532):

```bash
mkdir -p ./data/config ./data/user-avatars
chown -R 65532:65532 ./data/user-avatars
```

Then replace the volume flags:

```bash
-v ./data/config:/app/data/config:ro \
-v ./data/user-avatars:/app/data/user-avatars \
```

### Docker Compose

A ready-to-use [`compose.yml`](compose.yml) is included.

```bash
docker compose up -d
```

## Reverse proxy / subfolder deployment

The app fully supports running behind a reverse proxy (nginx, Caddy, Traefik, etc.) and
under a subfolder path (e.g. `https://example.com/avatar/`). It honours
`X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, and
`X-Forwarded-Prefix` automatically.

Relevant guides:

- **[Nginx Reverse Proxy](docs/nginx-reverse-proxy.md)**: full nginx config with TLS
  termination, SSE support, and optional direct avatar file serving
- **[Subfolder Deployment](docs/subfolder-deployment.md)**: hosting under a URL path
  prefix (e.g. `/avatar/`)

## How it works

```text
┌─────────┐        HTTPS        ┌───────────────┐     HTTP/HTTPS    ┌──────────────────────┐
│ Browser │ ◄─────────────────► │ Reverse Proxy │ ◄───────────────► │       Akvatar        │
└─────────┘                     │ (nginx/Caddy) │                   │   (Flask/gunicorn)   │
                                └───────────────┘                   └──────────┬───────────┘
                                                                               │
                        ┌──────────────────────────────────────────────────────┼────────┐
                        │                                                      │        │
                        ▼                                                      ▼        ▼
               ┌─────────────────┐                                    ┌──────────┐  ┌──────────┐
               │    Authentik    │                                    │   Disk   │  │   LDAP   │
               │  (OIDC + API)   │                                    │ (avatars)│  │(optional)│
               └─────────────────┘                                    └──────────┘  └──────────┘
```

1. User visits the app and clicks **Sign in**
2. OIDC redirect → Authentik login → callback stores user info and PK in session
3. Dashboard shows the user's current name and profile picture
4. User picks an image → Cropper.js enforces a square crop in the browser →
   compressed to WebP/JPEG via `canvas.toBlob()`
5. Cropped image is uploaded to `POST /api/upload`
6. Server validates (extension, magic bytes, Pillow decode, dimensions), strips all
   metadata, then resizes to all configured sizes, and saves as JPEG + PNG + WebP
7. Server `PATCH`es the `avatar-url` attribute on the Authentik user via the Admin API
8. *(If LDAP enabled)* Server writes the photo into configured LDAP attributes (binary
   bytes or URL string)
9. Browser shows step-by-step progress in real time via Server-Sent Events
10. Cleanup job runs on a cron schedule to remove deleted users' avatars, enforce
    per-user retention, and purge orphaned files

For a full walkthrough with sequence diagrams and cleanup details, see
**[How It Works](docs/how-it-works.md)**.

## Documentation

| Guide                                                  | Description                                                                             |
|--------------------------------------------------------|-----------------------------------------------------------------------------------------|
| [Configuration](docs/configuration.md)                 | Complete reference for all `config.yml` settings with defaults and explanations         |
| [How It Works](docs/how-it-works.md)                   | Full lifecycle walkthrough with sequence diagrams and cleanup flow                      |
| [Flask Session Key](docs/flask-session-key.md)         | Generating and setting the Flask session secret key                                     |
| [Authentik OIDC Setup](docs/authentik-oidc-setup.md)   | Creating the OIDC provider and application in Authentik                                 |
| [Authentik API Token](docs/authentik-api-token.md)     | Creating an API token for the Authentik Admin API                                       |
| [TLS](docs/tls.md)                                     | TLS certificate configuration and reverse proxy recommendation                          |
| [Nginx Reverse Proxy](docs/nginx-reverse-proxy.md)     | Full nginx config with TLS termination, SSE support, and optional static avatar serving |
| [Subfolder Deployment](docs/subfolder-deployment.md)   | Hosting the app under a URL path prefix (e.g. `/avatar/`)                               |
| [MS AD Service Account](docs/ms-ad-service-account.md) | Least-privilege Active Directory service account setup with PowerShell automation       |
| [Troubleshooting](docs/troubleshooting.md)             | General debugging tips, known issues, and their fixes                                   |
