# TLS Configuration

This guide covers how to configure TLS (HTTPS) for Akvatar.

## Recommended: Use a reverse proxy

For production deployments, **terminate TLS at a reverse proxy** (nginx, Caddy, Traefik, HAProxy) in front of the
application. This is the only officially supported and tested deployment method.

Benefits of reverse proxy TLS termination:

- **Automated certificate management:** Tools like Certbot (Let's Encrypt) or Caddy's built-in ACME integrate directly
  with the reverse proxy
- **Centralised TLS configuration:** Cipher suites, protocol versions, HSTS headers, and OCSP stapling are managed in
  one place
- **Better performance:** Reverse proxies are optimized for TLS handling and connection management
- **Separation of concerns:** The Python application does not need to handle TLS, reducing its attack surface

When the reverse proxy terminates TLS, the application runs over plain HTTP internally. The `X-Forwarded-Proto` header
tells the app that the original request was HTTPS, ensuring all generated URLs use the correct scheme.

See [Nginx Reverse Proxy](nginx-reverse-proxy.md) for a complete nginx configuration guide.

## Built-in TLS (development / testing)

The application can serve HTTPS directly using its built-in server. This is intended for 
**development and testing only**, not for production use.

Set the certificate and key paths in `data/config/config.yml`:

```yaml
webserver:
  tls_cert: "/path/to/cert.pem"
  tls_key: "/path/to/key.pem"
```

The server uses the same port for HTTPS. When TLS is not configured, a warning is logged at startup.

See [Configuration Reference](configuration.md#webservertls_cert--tls_key) for details.

## Generating a self-signed certificate

For development or testing, generate a self-signed certificate:

```bash
openssl req -x509 -newkey rsa:3072 -nodes \
  -keyout key.pem -out cert.pem \
  -days 3650 -subj "/CN=akvatar"
```

| Parameter          | Purpose                                             |
|--------------------|-----------------------------------------------------|
| `-x509`            | Generate a self-signed certificate (not a CSR)      |
| `-newkey rsa:3072` | Create a new 3072-bit RSA private key               |
| `-nodes`           | Do not encrypt the private key with a passphrase    |
| `-days 3650`       | Certificate validity period (10 years)              |
| `-subj "/CN=..."`  | Set the Common Name (CN) in the certificate subject |

Place the generated `cert.pem` and `key.pem` files in a secure location and reference them in `config.yml`.

Browsers will show a security warning for self-signed certificates. This is expected and can be bypassed for
development.

## Why not both?

If a reverse proxy terminates TLS, there is no need to also configure TLS on the application. Leave `webserver.tls_cert`
and `webserver.tls_key` empty. Running TLS on both layers adds unnecessary overhead and complexity without improving
security (the connection between the reverse proxy and the app is typically on the same host or a trusted network).
