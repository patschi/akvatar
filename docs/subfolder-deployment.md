# Subfolder Deployment

This guide explains how to host the Authentik Avatar Updater under a URL path prefix (e.g. `https://portal.example.com/avatar/`) instead of at the root of a domain.

## Overview

When the application runs behind a reverse proxy under a subfolder, it needs to know the path prefix so that all generated URLs (page links, static assets, OIDC callbacks, API endpoints) include the correct path. There are two ways to configure this.

## Option A: Reverse proxy sends `X-Forwarded-Prefix` (recommended)

Configure your reverse proxy to pass the `X-Forwarded-Prefix` header. The application detects this header automatically via Werkzeug's `ProxyFix` middleware and prepends the prefix to all generated URLs.

### nginx example

```nginx
server {
    listen 443 ssl http2;
    server_name portal.example.com;

    ssl_certificate     /etc/ssl/certs/portal.example.com.pem;
    ssl_certificate_key /etc/ssl/private/portal.example.com.key;

    location /avatar/ {
        # The trailing slash on proxy_pass strips the /avatar/ prefix
        # before forwarding to the app.
        proxy_pass http://127.0.0.1:5000/;

        proxy_set_header Host               $host;
        proxy_set_header X-Real-IP          $remote_addr;
        proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto  $scheme;
        proxy_set_header X-Forwarded-Prefix /avatar;

        # SSE support (required for upload progress)
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

Key points:
- `proxy_pass http://127.0.0.1:5000/;` -- the **trailing slash** strips `/avatar/` from the path before forwarding. The app receives requests at `/`, `/api/upload`, etc.
- `X-Forwarded-Prefix /avatar` -- tells the app to prepend `/avatar` to all generated URLs.

### No config.yml changes needed for `base_path`

When using `X-Forwarded-Prefix`, leave `webserver.base_path` unset or empty. The prefix is determined dynamically from the header.

## Option B: Set `base_path` in config.yml

If your reverse proxy does not support sending `X-Forwarded-Prefix`, set the path prefix statically in `config.yml`:

```yaml
webserver:
  base_path: "/avatar"
```

The application applies this prefix using a WSGI middleware that wraps all routes under the given path. When `base_path` is set, the app expects to receive requests with the prefix included (e.g. `/avatar/`, `/avatar/api/upload`), so the reverse proxy must **not** strip it:

```nginx
location /avatar/ {
    # No trailing slash on proxy_pass -- the /avatar/ prefix is kept.
    proxy_pass http://127.0.0.1:5000;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering    off;
    proxy_cache        off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

## Required config.yml settings

Regardless of which option you choose, the following settings in `config.yml` must reflect the full public URL including the subfolder:

```yaml
app:
  # Full URL users use to reach the app (including subfolder path)
  public_base_url: "https://portal.example.com/avatar"

  # Full URL where avatar files are served (including subfolder path)
  public_avatar_url: "https://portal.example.com/avatar/user-avatars"
```

## OIDC redirect URI

The OIDC callback path is `/callback`. When hosted under a subfolder, the full redirect URI becomes:

```
https://portal.example.com/avatar/callback
```

Update the **Redirect URIs/Origins** in your Authentik OAuth2/OpenID Provider to match this URL. See [Authentik OIDC Setup](authentik-oidc-setup.md) for details.

## How it works internally

The application uses two middleware layers (applied during startup in `app.py`):

1. **`ProxyFix`** (Werkzeug) -- trusts `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix` headers from the reverse proxy.
2. **`PrefixMiddleware`** -- if `webserver.base_path` is set, this WSGI middleware strips the prefix from incoming request paths and sets `SCRIPT_NAME` so Flask generates correct URLs.

The two approaches can coexist: if both `X-Forwarded-Prefix` and `base_path` are set, the proxy header takes precedence for URL generation while `base_path` handles path routing.

## Verifying the setup

After deploying, verify that:

1. **The login page loads** at `https://portal.example.com/avatar/`
2. **Static assets load** (CSS, JS, favicon) -- check the browser's Network tab for 404 errors
3. **OIDC login works** -- clicking "Sign in" redirects to Authentik and back to `/avatar/callback`
4. **Upload progress works** -- SSE events stream in real time during image upload
