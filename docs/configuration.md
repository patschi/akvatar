# Configuration Reference

All settings are defined in `data/config/config.yml`. Copy the example file to get started:

```bash
cp data/config/config.example.yml data/config/config.yml
```

The application reads the configuration file once at startup. Changes require a restart to take effect.

---

## Dry-Run Mode

### `dry_run`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `false` |

When enabled, avatar images are still processed and saved to disk, but no changes are pushed to Authentik or LDAP. All operations that would have been performed are logged instead. Useful for testing the full upload pipeline without affecting real user accounts.

---

## Branding

### `branding.name`

| | |
|---|---|
| **Type** | String |
| **Default** | `"Avatar Updater"` |

The application name displayed in the browser title bar and the navigation header. Change this to match your organisation's branding (e.g. `"Contoso Avatar Updater"`).

---

## Application

### `app.secret_key`

| | |
|---|---|
| **Type** | String |
| **Default** | `"CHANGE-ME-to-a-random-secret-key"` (placeholder -- must be changed) |

The secret key used by Flask to cryptographically sign session cookies. If this key is predictable or too short, an attacker can forge sessions.

The application **refuses to start** if:
- The key is still set to the default placeholder value
- The key is shorter than 16 characters

See [Flask Session Key](flask-session-key.md) for generation instructions.

### `app.max_upload_size_mb`

| | |
|---|---|
| **Type** | Integer |
| **Default** | `10` |

Maximum allowed upload size in megabytes. Files exceeding this limit are rejected by Flask before reaching the upload handler. Note that the browser compresses images client-side before uploading, so typical uploads are well under 1 MB regardless of this limit.

### `app.avatar_storage_path`

| | |
|---|---|
| **Type** | String (file path) |
| **Default** | `"data/user-avatars"` |

Directory where processed avatar images and metadata are stored. Can be a relative path (relative to the project root) or an absolute path. The application creates the directory and all required subdirectories at startup if they do not exist.

### `app.public_base_url`

| | |
|---|---|
| **Type** | String (URL) |
| **Default** | `"https://avatar.example.com"` (must be changed) |

The full public URL where users access the application. Used to generate OIDC redirect URIs and other external links.

If the URL includes a path component (e.g. `https://portal.example.com/avatar`), the application automatically serves under that subfolder. See [Subfolder Deployment](subfolder-deployment.md).

Must **not** have a trailing slash.

### `app.public_avatar_url`

| | |
|---|---|
| **Type** | String (URL) |
| **Default** | `"https://avatar.example.com/user-avatars"` (must be changed) |

The public base URL where avatar files are accessible. This URL is used to build the avatar URL that is pushed to Authentik and LDAP. It must point to the location where the files from `avatar_storage_path` are served (either by the application itself or by a reverse proxy serving them directly).

Must **not** have a trailing slash.

### `app.avatar_retention_count`

| | |
|---|---|
| **Type** | Integer |
| **Default** | `2` |

Number of avatar sets to keep per user. When a user has more than this many uploaded avatars, the cleanup job deletes the oldest ones. Set to `0` to keep all uploads indefinitely (no retention cleanup).

### `app.cleanup_interval`

| | |
|---|---|
| **Type** | String (cron expression) |
| **Default** | `"0 2 * * *"` (daily at 2:00 AM) |

Cron schedule for the cleanup job that removes avatars of deleted users and enforces per-user retention limits. Uses standard 5-field crontab syntax (`minute hour day month weekday`).

Set to `""` (empty string) to disable the cleanup job entirely.

### `app.cleanup_on_startup`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `false` |

When enabled, the cleanup job runs once 60 seconds after application startup, in addition to the regular cron schedule. Useful for catching up after extended downtime.

### `app.log_level`

| | |
|---|---|
| **Type** | String (enum) |
| **Default** | `"INFO"` |
| **Valid values** | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

Controls the verbosity of log output:

| Level | What gets logged |
|---|---|
| `DEBUG` | Every action: OIDC flow, image resize steps, LDAP bind, API calls, HTTP requests to non-static assets, uploaded image metadata |
| `INFO` | Key events: login, upload, save, API/LDAP updates |
| `WARNING` | Rejected uploads, missing TLS configuration, LDAP without SSL |
| `ERROR` | Failures in Authentik API or LDAP calls |
| `CRITICAL` | Fatal startup errors (missing config, etc.) |

### `app.debug_full`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `false` |

Enables full debug mode. When active:
- Flask debugger is enabled (interactive traceback pages in the browser)
- Log level is forced to `DEBUG` regardless of `app.log_level`
- Template auto-reload is enabled (templates are re-read from disk on every request)

**Never enable in production.** A warning is logged at startup when this is active.

---

## Webserver

These settings apply when running via `run.py` / gunicorn (production and Docker). When running via `app.py` (development), only `host`, `port`, and TLS settings are used.

### `webserver.access_log`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `false` |

When enabled, every HTTP request is logged to the console (except requests to `/static/` assets). Useful for debugging but verbose in production.

### `webserver.workers`

| | |
|---|---|
| **Type** | Integer |
| **Default** | `2` |

Number of gunicorn worker processes. Each worker is an independent OS process that handles requests. A reasonable starting point is `2 * CPU_cores + 1`, but for this application 2-4 workers are usually sufficient since image processing is the bottleneck, not concurrency.

### `webserver.threads`

| | |
|---|---|
| **Type** | Integer |
| **Default** | `4` |

Number of threads per worker. Each thread handles one request concurrently within a worker process. Threads help handle idle connections (e.g. SSE streams) without blocking the worker. The gunicorn worker class is `gthread` (threaded workers).

### `webserver.timeout`

| | |
|---|---|
| **Type** | Integer (seconds) |
| **Default** | `120` |

Worker timeout in seconds. A worker is restarted by gunicorn if it does not respond to the arbiter within this duration. Should be long enough for the slowest expected request (e.g. large image upload + processing + LDAP update). If you see `WORKER TIMEOUT` errors, increase this value.

### `webserver.tls_cert` / `tls_key`

| | |
|---|---|
| **Type** | String (file path) |
| **Default** | `""` (empty -- no TLS) |

Paths to the TLS certificate and private key files for HTTPS. When both are set, the server uses HTTPS on the same port. When empty, the server runs over plain HTTP and a warning is logged at startup.

For production, terminate TLS at a reverse proxy instead. See [TLS Configuration](tls.md).

---

## OpenID Connect / Authentik Login

See [Authentik OIDC Setup](authentik-oidc-setup.md) for step-by-step setup instructions.

### `oidc.issuer_url`

| | |
|---|---|
| **Type** | String (URL) |
| **Default** | `"https://auth.example.com/application/o/avatar-updater"` (must be changed) |

The Authentik OpenID provider URL. Follows the pattern `https://<authentik-domain>/application/o/<application-slug>`. The application appends `/.well-known/openid-configuration` to discover endpoints automatically.

Must **not** have a trailing slash.

### `oidc.client_id`

| | |
|---|---|
| **Type** | String |
| **Default** | `"avatar-updater"` (must be changed) |

The OAuth2 client ID from the Authentik provider configuration.

### `oidc.client_secret`

| | |
|---|---|
| **Type** | String |
| **Default** | `"CHANGE-ME"` (must be changed) |

The OAuth2 client secret from the Authentik provider configuration. Treat as a secret -- do not commit to version control.

### `oidc.username_claim`

| | |
|---|---|
| **Type** | String |
| **Default** | `"preferred_username"` |

The OIDC claim that carries the unique username. The value of this claim is used to look up the user via the Authentik API. In most Authentik setups, `preferred_username` is correct.

---

## Authentik Admin API

See [Authentik API Token](authentik-api-token.md) for step-by-step setup instructions.

### `authentik_api.base_url`

| | |
|---|---|
| **Type** | String (URL) |
| **Default** | `"https://auth.example.com"` (must be changed) |

The base URL of your Authentik instance (without trailing slash). The application appends API paths like `/api/v3/core/users/` to this URL.

### `authentik_api.api_token`

| | |
|---|---|
| **Type** | String |
| **Default** | `"CHANGE-ME"` (must be changed) |

An API token with permissions to read and write user attributes. See [Authentik API Token](authentik-api-token.md) for required permissions.

### `authentik_api.avatar_size`

| | |
|---|---|
| **Type** | Integer (pixels) |
| **Default** | `1024` |

Which generated image size (in pixels) to use for the avatar URL pushed to Authentik. This value **must** be one of the entries in `images.sizes`. The application validates this at startup and exits with an error if the size is not found.

### `authentik_api.avatar_attribute`

| | |
|---|---|
| **Type** | String |
| **Default** | `"avatar-url"` |

The Authentik user attribute name where the avatar URL is stored. The application sets `attributes.<this-value>` on the user object via the API. Change this if your Authentik configuration uses a different attribute name for avatar URLs.

---

## LDAP Server (optional)

Supports any standards-compliant LDAP server. Microsoft Active Directory is the primary and only tested target. See [MS AD Service Account](ms-ad-service-account.md) for setting up a least-privilege service account in Active Directory.

### `ldap.enabled`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `false` |

Set to `true` to enable LDAP thumbnail updates. When disabled, the entire LDAP module is a no-op and no LDAP connections are made.

### `ldap.server`

| | |
|---|---|
| **Type** | String |
| **Default** | `"ldaps://dc.example.com"` |

The LDAP server address. Use `ldaps://` for LDAP over TLS (recommended) or `ldap://` for plain LDAP.

### `ldap.port`

| | |
|---|---|
| **Type** | Integer |
| **Default** | `636` |

The LDAP server port. Standard ports: `636` for LDAPS, `389` for LDAP.

### `ldap.use_ssl`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `true` |

Whether to use SSL/TLS for the LDAP connection. Should be `true` when using port 636. A warning is logged at startup if LDAP is enabled without SSL.

### `ldap.skip_cert_verify`

| | |
|---|---|
| **Type** | Boolean |
| **Default** | `false` |

Set to `true` to skip TLS certificate verification. Use only for self-signed certificates in development environments. A warning is logged at startup when this is enabled, as it makes the connection vulnerable to MITM attacks.

### `ldap.bind_dn`

| | |
|---|---|
| **Type** | String |
| **Default** | `"CN=svc-avatar,OU=Service Accounts,DC=example,DC=com"` |

The distinguished name (DN) of the service account used to bind (authenticate) to the LDAP server. This account needs read access to search for users and write access to the photo attribute.

### `ldap.bind_password`

| | |
|---|---|
| **Type** | String |
| **Default** | `"CHANGE-ME"` (must be changed) |

The password for the LDAP bind DN. Treat as a secret.

### `ldap.search_base`

| | |
|---|---|
| **Type** | String |
| **Default** | `"DC=example,DC=com"` |

The base DN under which user objects are searched. The search is performed with subtree scope, so users in any sub-OU are found.

### `ldap.search_filter`

| | |
|---|---|
| **Type** | String |
| **Default** | `"(objectSid={ldap_uniq})"` |

The LDAP search filter used to locate the user object. The placeholder `{ldap_uniq}` is replaced with the user's `ldap_uniq` attribute value from Authentik (properly escaped for LDAP filter syntax).

The default uses `objectSid`, which is the standard unique identifier in Microsoft Active Directory. For other LDAP directories, change this to match your schema (e.g. `(uid={ldap_uniq})` for OpenLDAP).

### `ldap.photo_attribute`

| | |
|---|---|
| **Type** | String |
| **Default** | `"thumbnailPhoto"` |

The LDAP attribute to write the photo JPEG bytes into. The default `thumbnailPhoto` is standard in Microsoft Active Directory and is displayed in Outlook, Teams, and other Microsoft applications. For other directories, use the appropriate attribute (e.g. `jpegPhoto` for OpenLDAP).

### `ldap.max_thumbnail_kb`

| | |
|---|---|
| **Type** | Integer (kilobytes) |
| **Default** | `100` |

Maximum allowed size of the JPEG thumbnail in kilobytes. The application checks the thumbnail size before writing it to LDAP and raises an error if it exceeds this limit. Active Directory has a default limit of ~100 KB for `thumbnailPhoto`.

### `ldap.thumbnail_size`

| | |
|---|---|
| **Type** | Integer (pixels) |
| **Default** | `128` |

Which generated image size (in pixels) to use for the LDAP photo JPEG. This value **must** be one of the entries in `images.sizes`. The application validates this at startup and exits with an error if the size is not found.

A smaller size (128 or 256) is recommended for LDAP to stay within the `max_thumbnail_kb` limit and because LDAP photo attributes are typically used for small thumbnails.

---

## Image Processing

### `images.sizes`

| | |
|---|---|
| **Type** | List of integers |
| **Default** | `[1024, 648, 512, 256, 128, 64]` |

The square pixel dimensions to generate for each uploaded avatar. Every uploaded image is resized to each of these sizes. The values in `authentik_api.avatar_size` and `ldap.thumbnail_size` must appear in this list.

### `images.formats`

| | |
|---|---|
| **Type** | List of strings |
| **Default** | `["jpg", "png", "webp"]` |

The output formats to save for each size. Each size x format combination produces one file. Supported values: `jpg` (JPEG), `png`, `webp`.

### `images.jpeg_quality`

| | |
|---|---|
| **Type** | Integer (1--100) |
| **Default** | `90` |

JPEG compression quality. Higher values produce better quality but larger files. 90 is a good balance for avatars.

### `images.webp_quality`

| | |
|---|---|
| **Type** | Integer (1--100) |
| **Default** | `85` |

WebP compression quality. Similar to JPEG quality but WebP typically achieves better compression at the same visual quality.

### `images.png_compress_level`

| | |
|---|---|
| **Type** | Integer (0--9) |
| **Default** | `6` |

PNG compression level. Higher values produce smaller files but take longer to compress. 6 is the default balance. PNG compression is lossless, so this only affects file size and compression speed, not image quality.
