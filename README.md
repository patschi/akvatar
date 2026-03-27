# Authentik Avatar Updater

A self-hosted web application that lets users update their profile picture via a modern browser UI. The image is cropped client-side, processed into multiple sizes and formats on server-side, and then respective URLs pushed to **Authentik** (via API) and optionally to **Microsoft Active Directory/LDAP Server**.

## Features

- **OpenID Connect login** via Authentik (scopes: `openid profile email`)
- **Multiple Languages**: Automatically retrieved through OIDC `locale` attribute in `profile` scope. Currently supported English (default), German.
- **Client-side cropping** with [Cropper.js](https://github.com/fengyuanchen/cropperjs) (bundled locally, no external CDN)
- **Multi-size output** -- 1024, 648, 512, 256, 128, 64 (configurable)
- **Multi-format output** -- JPEG, PNG, WebP (configurable)
- **Unguessable filenames** (`uuid4` + `token_urlsafe` + nanosecond timestamp)
- **Authentik API** -- sets `attributes.avatar-url` on the user object (configurable)
- **LDAP Server** -- writes `thumbnailPhoto` (optional, toggle in config)
- **JSON metadata** -- saves upload metadata (username, timestamp, sizes) per avatar
- **Configurable branding** -- customise the application name in the UI
- **Reverse proxy / subfolder support** -- respects `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-Prefix`
- **Optional TLS** -- serve HTTPS directly from the built-in server
- **Distroless Docker image** for minimal attack surface

## Project structure

```text
authentik-avatar-updater/
├── app.py                  # Entry point & Flask app factory
├── requirements.txt        # Python dependencies
├── Dockerfile              # Distroless container build
├── .dockerignore
├── src/
│   ├── __init__.py
│   ├── config.py           # YAML config loading & logging setup
│   ├── auth.py             # OIDC login/logout/callback routes
│   ├── routes.py           # Page routes & upload API
│   ├── imaging.py          # Resize, compress, save images
│   ├── authentik_api.py    # Authentik REST API client
│   └── ldap_client.py      # Active Directory LDAP client
├── templates/
│   ├── login.html          # Public landing page
│   ├── logged_out.html     # Post-logout page
│   └── dashboard.html      # Authenticated upload page
├── static/
│   ├── style.css           # Application stylesheet
│   ├── logo.svg            # Application logo
│   ├── favicon.svg         # SVG favicon
│   ├── favicon.ico         # ICO favicon (legacy browsers)
│   ├── favicon-192.png     # PNG icon (apple-touch-icon)
│   └── vendor/
│       ├── cropper.min.css # Cropper.js (bundled)
│       └── cropper.min.js
└── data/
    ├── config/
    │   ├── config.example.yml  # Example settings (copy to config.yml)
    │   └── config.yml          # Your configuration (not committed)
    └── user-avatars/           # Generated avatars (created at runtime)
```

## Prerequisites

- Linux (Debian, Ubuntu, RHEL, Alpine, etc.)
- Python 3.11+
- An Authentik instance (see [Authentik setup](#authentik-setup) below)
- *(Optional)* A Microsoft Active Directory domain controller reachable via LDAPS/LDAP

## Quick start

### 1. Clone the repository

```bash
git clone <repo-url> authentik-avatar-updater
cd authentik-avatar-updater
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

```bash
cp data/config/config.example.yml data/config/config.yml
```

#### Generate a Flask session secret key

The `app.secret_key` value must be a long, random string. Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output into `data/config/config.yml` under `app.secret_key`.

#### Key settings

Edit `data/config/config.yml` with your environment's values:

| Setting | Purpose |
|---|---|
| `branding.name` | Application name shown in the browser title and navigation bar |
| `app.secret_key` | Random string for Flask sessions (see above) |
| `app.public_base_url` | The URL users reach the app at |
| `app.public_avatar_url` | The public URL where avatar files are served (used in Authentik/LDAP) |
| `app.log_level` | `DEBUG` for development, `INFO` for production |
| `webserver.host` / `port` | Bind address and port for the built-in server |
| `webserver.base_path` | Set when hosting under a subfolder (e.g. `/avatar`) |
| `webserver.tls_cert` / `tls_key` | Paths to TLS cert/key for HTTPS |
| `oidc.*` | Your Authentik OIDC provider details |
| `authentik_api.*` | Authentik base URL and API token |
| `authentik_api.avatar_size` | Image size (px) used for the Authentik avatar URL (must be in `images.sizes`) |
| `authentik_api.avatar_attribute` | Authentik user attribute to store the avatar URL in (default: `avatar-url`) |
| `ldap.enabled` | `true` to enable AD updates, `false` to skip |
| `ldap.skip_cert_verify` | `true` to skip TLS certificate verification (e.g. self-signed certs) |
| `ldap.thumbnail_size` | Image size (px) used for AD thumbnailPhoto (must be in `images.sizes`) |
| `ldap.*` | AD connection details (only needed when enabled) |

### 5. Run

```bash
python app.py
```

The app starts on `http://0.0.0.0:5000` by default.

For development with auto-reload:

```bash
FLASK_DEBUG=1 python app.py
```

## Authentik setup

Follow these steps to create the required Application and Provider in Authentik.

### 1. Create a Signing Key (if you don't have one)

1. In the Authentik admin panel, go to **System > Certificates**
2. Click **Generate** to create a new self-signed certificate/key pair
3. Give it a name (e.g. `OIDC Signing Key`) and save
4. This key will be used by the OIDC provider to sign ID tokens

### 2. Create an OAuth2/OpenID Provider

1. Go to **Applications > Providers** and click **Create**
2. Select **OAuth2/OpenID Provider**
3. Fill in:
   - **Name**: e.g. `Avatar Updater`
   - **Authorization flow**: select your standard authorization flow
   - **Client ID**: note the auto-generated value (or set your own) -- this goes into `oidc.client_id`
   - **Client Secret**: note the auto-generated value -- this goes into `oidc.client_secret`
   - **Redirect URIs/Origins**: set to `https://your-app-url/callback` (the `/callback` path is required)
   - **Signing Key**: select the certificate you created in step 1
4. Under **Advanced protocol settings**:
   - **Scopes**: ensure `openid`, `profile`, and `email` are selected
   - **Subject mode**: can be left at the default ("Based on the hashed User ID") -- the app looks up users by username, not by `sub` claim
5. Save the provider

### 3. Create an Application

1. Go to **Applications > Applications** and click **Create**
2. Fill in:
   - **Name**: e.g. `Avatar Updater`
   - **Slug**: e.g. `avatar-updater`
   - **Provider**: select the OAuth2/OpenID Provider you just created
3. Save the application

### 4. Create an API Token

1. Go to **Directory > Tokens and App passwords** and click **Create**
2. Set **Intent** to **API Token**
3. Assign it to a user that has permissions to read and write user attributes (typically an admin or a service account)
4. Copy the token value -- this goes into `authentik_api.api_token`

### 5. Fill in the config

Using the values from above, fill in `data/config/config.yml`:

```yaml
oidc:
  issuer_url: "https://your-authentik-domain/application/o/avatar-updater"
  client_id: "<client-id-from-step-2>"
  client_secret: "<client-secret-from-step-2>"

authentik_api:
  base_url: "https://your-authentik-domain"
  api_token: "<token-from-step-4>"
```

The `issuer_url` follows the pattern `https://<authentik-domain>/application/o/<application-slug>`.

## Reverse proxy / subfolder deployment

The app fully supports running behind a reverse proxy under a subfolder (e.g. `https://example.com/avatar/`).

**Option A -- reverse proxy sets the prefix** (recommended):

Configure your reverse proxy to pass the `X-Forwarded-Prefix` header. The app honours `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix` automatically via Werkzeug `ProxyFix`.

Example nginx snippet:

```nginx
location /avatar/ {
    proxy_pass http://127.0.0.1:5000/;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /avatar;
}
```

**Option B -- set `base_path` in config**:

If your reverse proxy does not send `X-Forwarded-Prefix`, set `webserver.base_path` in `data/config/config.yml`:

```yaml
webserver:
  base_path: "/avatar"
```

## TLS

To serve HTTPS directly (without a reverse proxy terminating TLS), set the certificate and key paths in `data/config/config.yml`:

```yaml
webserver:
  tls_cert: "/path/to/cert.pem"
  tls_key: "/path/to/key.pem"
```

The server uses the same port for HTTPS. A warning is logged at startup when TLS is not configured.

To generate a self-signed certificate for development/testing:

```bash
openssl req -x509 -newkey rsa:3072 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=localhost"
```

## Running with Docker

### Build the image

```bash
docker build -t avatar-updater .
```

### Run the container

```bash
docker run -d \
  --name avatar-updater \
  -p 5000:5000 \
  -v /path/to/your/config.yml:/app/data/config/config.yml:ro \
  -v avatar-data:/app/data/user-avatars \
  avatar-updater
```

- Mount your `config.yml` at `/app/data/config/config.yml` (or set `CONFIG_PATH` env var)
- Mount a volume at `/app/data/user-avatars` for persistent avatar storage

### Override config path

```bash
docker run -e CONFIG_PATH=/etc/avatar/config.yml ...
```

### Docker Compose

```yml
services:
  avatar-updater:
    build: .
    container_name: avatar-updater
    restart: unless-stopped
    security_opt:
      - no-new-privileges
    ports:
      - "5000:5000"
    volumes:
      - ./data/config/config.yml:/app/data/config/config.yml:ro
      - avatar-data:/app/data/user-avatars

volumes:
  avatar-data:
```

Start with:

```bash
docker compose up -d
```

## Logging

The log level is configured in `data/config/config.yml` under `app.log_level`. Supported values:

| Level | What gets logged |
|---|---|
| `DEBUG` | Every action: OIDC flow, image resize steps, LDAP bind, API calls, HTTP requests to non-static assets, uploaded image metadata |
| `INFO` | Key events: login, upload, save, API/LDAP updates |
| `WARNING` | Rejected uploads, missing TLS configuration, LDAP without SSL |
| `ERROR` | Failures in Authentik API or LDAP calls |
| `CRITICAL` | Fatal startup errors (missing config, etc.) |

Logs are written to stdout in the format:

```
2025-01-15 10:32:01 [INFO    ] routes                   | Upload request from user 'jdoe'.
2025-01-15 10:32:01 [DEBUG   ] http                     | POST /api/upload 200 (client=10.0.0.42)
```

## Configuration reference

See the comments in [`config.example.yml`](data/config/config.example.yml) for a full description of every setting.

## How it works

1. User visits the app and clicks **Sign in with Authentik**
2. OIDC redirect -> Authentik login -> callback stores user info in session
3. Dashboard shows the user's current name and profile picture
4. User picks an image -> Cropper.js enforces a square crop in the browser
5. Cropped image is uploaded as PNG to `POST /api/upload`
6. Server resizes to all configured sizes, saves as JPG + PNG + WebP
7. Server PATCHes `attributes.avatar-url` on the Authentik user via API
8. *(If LDAP enabled)* Server writes the thumbnail JPEG into `thumbnailPhoto` in AD
9. A JSON metadata file is saved alongside the avatar files
10. Browser shows step-by-step progress with success/fail status
