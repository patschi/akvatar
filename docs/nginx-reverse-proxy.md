# Nginx Reverse Proxy

This guide covers how to run Akvatar behind an **nginx** reverse proxy.

## Prerequisites

- nginx 1.18+ installed and running
- The Avatar Updater running on `127.0.0.1:5000` (default)
- A DNS record pointing to your nginx server

## Basic configuration

The application relies on standard proxy headers to determine the original client address, protocol, and host. nginx
must forward these headers so that Flask generates correct URLs and logs the real client IP.

```nginx
server {
    listen 443 ssl http2;
    server_name avatar.example.com;

    ssl_certificate     /etc/ssl/certs/avatar.example.com.pem;
    ssl_certificate_key /etc/ssl/private/avatar.example.com.key;

    location / {
        proxy_pass http://127.0.0.1:5000;

        # --- Required proxy headers ---
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # --- SSE (Server-Sent Events) support ---
        # The upload endpoint streams real-time progress via SSE.
        # Buffering must be disabled so events reach the browser immediately.
        proxy_buffering off;
        proxy_cache     off;

        # Keep the connection open long enough for image processing to complete.
        # Match or exceed the gunicorn timeout (default 120s in config.yml).
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

## Key headers explained

| Header               | Purpose                                                               |
|----------------------|-----------------------------------------------------------------------|
| `Host`               | Preserves the original `Host` header so Flask generates correct URLs  |
| `X-Real-IP`          | The actual client IP address                                          |
| `X-Forwarded-For`    | Appends the client IP to the proxy chain (used by Flask's `ProxyFix`) |
| `X-Forwarded-Proto`  | Tells the app whether the original request was HTTP or HTTPS          |
| `X-Forwarded-Prefix` | Only needed when hosting under a [subfolder](subfolder-deployment.md) |

## SSE considerations

The upload endpoint (`POST /api/upload`) returns a streaming response using Server-Sent Events (SSE). Each processing
step (validation, resizing, Authentik API update, LDAP update) is sent as an SSE event in real time.

If nginx buffers the response, the browser will not receive progress updates until the entire upload is complete. The
`proxy_buffering off` directive is essential.

The `proxy_read_timeout` should be set high enough to cover the entire upload and processing time. A value of 300
seconds is a safe default for large images with LDAP updates.

## Health check probe

The application exposes `GET /healthz` which returns `200 OK` with body `OK`. Use this
as a liveness probe for load balancers or container health checks — it requires no
authentication and performs no external calls.

```nginx
# Optional: expose the health check without proxying through the application
# (only useful if you want nginx to gate on it independently)
location = /healthz {
    proxy_pass         http://127.0.0.1:5000;
    proxy_set_header   Host $host;
    access_log         off;
}
```

## TLS termination

When nginx terminates TLS, there is no need to configure TLS in the Avatar Updater itself. Leave `webserver.tls.cert`
and `webserver.tls.key` empty in `config.yml` and let nginx handle certificates. See
[App TLS Configuration](app-tls.md) for more details.

The `X-Forwarded-Proto` header tells the app that the original request was HTTPS, ensuring all generated URLs
(redirects, OIDC callbacks) use the correct scheme.

## HTTP to HTTPS redirect

Add a server block to redirect plain HTTP to HTTPS:

```nginx
server {
    listen 80;
    server_name avatar.example.com;
    return 301 https://$host$request_uri;
}
```

## Upload size limit

nginx defaults to a 1 MB request body limit. The Avatar Updater compresses images client-side before uploading, so
typical uploads are well under 1 MB. However, if you want to match the application's configured limit:

```nginx
# Inside the server or location block
client_max_body_size 10m;  # Match app.max_upload_size_mb in config.yml
```

## Serving avatar files directly (optional)

If the avatar storage directory is accessible to nginx, you can serve avatar images directly from nginx instead of
proxying through the application. This reduces load on the Python process.

```nginx
# Serve avatar images directly from disk
location /user-avatars/ {
    alias /path/to/data/user-avatars/;
    expires 7d;
    add_header Cache-Control "public, immutable";
}

# Proxy everything else to the application
location / {
    proxy_pass http://127.0.0.1:5000;
    # ... proxy headers as above ...
}
```

When using this approach, set `app.public_avatar_url` in `config.yml` to `https://avatar.example.com/user-avatars`.

## Full example

A complete nginx configuration combining TLS termination, HTTP redirect, SSE support, and avatar file serving:

```nginx
# Redirect HTTP -> HTTPS
server {
    listen 80;
    server_name avatar.example.com;
    return 301 https://$host$request_uri;
}

# Main server block
server {
    listen 443 ssl http2;
    server_name avatar.example.com;

    ssl_certificate     /etc/ssl/certs/avatar.example.com.pem;
    ssl_certificate_key /etc/ssl/private/avatar.example.com.key;

    # Upload size limit (match config.yml app.max_upload_size_mb)
    client_max_body_size 10m;

    # Application
    location / {
        proxy_pass http://127.0.0.1:5000;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```
