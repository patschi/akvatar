# Configuration Reference

All settings are defined in `data/config/config.yml`. Copy the example file to get started:

```bash
cp data/config/config.example.yml data/config/config.yml
```

The application reads the configuration file once at startup. Changes require a restart to take effect.

## Settings overview

| Setting                                                                 | Type    | Description                                         |
| ----------------------------------------------------------------------- | ------- | --------------------------------------------------- |
| [`dry_run`](#dry_run)                                                   | Boolean | Skip Authentik/LDAP writes; log all actions instead |
| [`branding.name`](#branding_name)                                       | String  | Application name shown in the UI                    |
| [`app.secret_key`](#app_secret_key)                                     | String  | Flask session signing key                           |
| [`app.max_upload_size_mb`](#app_max_upload_size_mb)                     | Integer | Maximum upload size in MB                           |
| [`app.avatar_storage_path`](#app_avatar_storage_path)                   | String  | Directory for stored avatar images                  |
| [`app.public_base_url`](#app_public_base_url)                           | URL     | Public URL where the application is reachable       |
| [`app.public_avatar_url`](#app_public_avatar_url)                       | URL     | Public URL where avatar files are served            |
| [`app.web_session_lifetime_seconds`](#app_web_session_lifetime_seconds) | Integer | Session cookie lifetime in seconds                  |
| [`cleanup.interval`](#cleanup_interval)                                 | Cron    | Cron schedule for the cleanup job                   |
| [`cleanup.on_startup`](#cleanup_on_startup)                             | Boolean | Run cleanup once 60 s after startup                 |
| [`cleanup.avatar_retention_count`](#cleanup_avatar_retention_count)     | Integer | Avatar sets to keep per user (0 = unlimited)        |
| [`cleanup.when_user_deleted`](#cleanup_when_user_deleted)               | Boolean | Remove avatars of users deleted from Authentik      |
| [`cleanup.when_user_deactivated`](#cleanup_when_user_deactivated)       | Boolean | Remove avatars of deactivated Authentik users       |
| [`app.log_level`](#app_log_level)                                       | Enum    | Log verbosity                                       |
| [`app.debug_full`](#app_debug_full)                                     | Boolean | Full debug mode — never enable in production        |
| [`webserver.proxy_mode`](#webserver_proxy_mode)                         | Boolean | Enable reverse-proxy header support (ProxyFix)      |
| [`webserver.access_log`](#webserver_access_log)                         | Boolean | Log every HTTP request to the console               |
| [`webserver.workers`](#webserver_workers)                               | Integer | Number of gunicorn worker processes                 |
| [`webserver.threads`](#webserver_threads)                               | Integer | Threads per worker                                  |
| [`webserver.timeout`](#webserver_timeout)                               | Integer | Worker timeout in seconds                           |
| [`webserver.tls_cert`](#webserver_tls_cert)                             | String  | Path to TLS certificate file                        |
| [`webserver.tls_key`](#webserver_tls_cert)                              | String  | Path to TLS private key file                        |
| [`oidc.issuer_url`](#oidc_issuer_url)                                   | URL     | Authentik OIDC provider URL                         |
| [`oidc.client_id`](#oidc_client_id)                                     | String  | OAuth2 client ID                                    |
| [`oidc.client_secret`](#oidc_client_secret)                             | String  | OAuth2 client secret                                |
| [`oidc.username_claim`](#oidc_username_claim)                           | String  | OIDC claim used as the username                     |
| [`authentik_api.base_url`](#authentik_api_base_url)                     | URL     | Authentik instance base URL                         |
| [`authentik_api.api_token`](#authentik_api_api_token)                   | String  | Authentik Admin API token                           |
| [`authentik_api.avatar_size`](#authentik_api_avatar_size)               | Integer | Image size (px) used for the Authentik avatar URL   |
| [`authentik_api.avatar_attribute`](#authentik_api_avatar_attribute)     | String  | Authentik user attribute to store the avatar URL    |
| [`ldap.enabled`](#ldap_enabled)                                         | Boolean | Enable LDAP photo attribute updates                 |
| [`ldap.server`](#ldap_server)                                           | String  | LDAP server address                                 |
| [`ldap.port`](#ldap_port)                                               | Integer | LDAP server port                                    |
| [`ldap.use_ssl`](#ldap_use_ssl)                                         | Boolean | Use SSL/TLS for the LDAP connection                 |
| [`ldap.skip_cert_verify`](#ldap_skip_cert_verify)                       | Boolean | Skip TLS certificate verification                   |
| [`ldap.bind_dn`](#ldap_bind_dn)                                         | String  | Service account DN for LDAP bind                    |
| [`ldap.bind_password`](#ldap_bind_password)                             | String  | Service account password                            |
| [`ldap.search_base`](#ldap_search_base)                                 | String  | Base DN for user searches                           |
| [`ldap.search_filter`](#ldap_search_filter)                             | String  | LDAP filter to locate the user object               |
| [`ldap.photos`](#ldap_photos)                                           | List    | LDAP photo attributes to update (see details below) |
| [`images.sizes`](#images_sizes)                                         | List    | Square output sizes to generate (px)                |
| [`images.formats`](#images_formats)                                     | List    | Output formats to save for each size                |
| [`images.jpeg_quality`](#images_jpeg_quality)                           | Integer | JPEG compression quality (1–100)                    |
| [`images.webp_quality`](#images_webp_quality)                           | Integer | WebP compression quality (1–100)                    |
| [`images.png_compress_level`](#images_png_compress_level)               | Integer | PNG compression level (0–9)                         |

---

## Dry-Run Mode

<a id="dry_run"></a>

### `dry_run`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, avatar images are still processed and saved to disk, but no changes are pushed to Authentik or LDAP. All operations that would have been performed are logged instead. Useful for testing the full upload pipeline without affecting real user accounts.

---

## Branding

<a id="branding_name"></a>

### `branding.name`

|             |                    |
| ----------- | ------------------ |
| **Type**    | String             |
| **Default** | `"Avatar Updater"` |

The application name displayed in the browser title bar and the navigation header. Change this to match your organisation's branding (e.g. `"Contoso Avatar Updater"`).

---

## Application

<a id="app_secret_key"></a>

### `app.secret_key`

|             |                                                                     |
| ----------- | ------------------------------------------------------------------- |
| **Type**    | String                                                              |
| **Default** | `"CHANGE-ME-to-a-random-secret-key"` (placeholder, must be changed) |

The secret key used by Flask to cryptographically sign session cookies. If this key is predictable or too short, an attacker can forge sessions.

The application **refuses to start** if:

- The key is still set to the default placeholder value
- The key is shorter than 32 characters

See [Flask Session Key](flask-session-key.md) for generation instructions.

<a id="app_max_upload_size_mb"></a>

### `app.max_upload_size_mb`

|             |         |
| ----------- | ------- |
| **Type**    | Integer |
| **Default** | `10`    |

Maximum allowed upload size in megabytes. Files exceeding this limit are rejected by Flask before reaching the upload handler. Note that the browser compresses images client-side before uploading, so typical uploads are well under 1 MB regardless of this limit.

<a id="app_avatar_storage_path"></a>

### `app.avatar_storage_path`

|             |                       |
| ----------- | --------------------- |
| **Type**    | String (file path)    |
| **Default** | `"data/user-avatars"` |

Directory where processed avatar images and metadata are stored. Can be a relative path (relative to the project root) or an absolute path. The application creates the directory and all required subdirectories at startup if they do not exist.

<a id="app_public_base_url"></a>

### `app.public_base_url`

|             |                                                  |
| ----------- | ------------------------------------------------ |
| **Type**    | String (URL)                                     |
| **Default** | `"https://avatar.example.com"` (must be changed) |

The full public URL where users access the application. Used to generate OIDC redirect URIs and other external links.

If the URL includes a path component (e.g. `https://portal.example.com/avatar`), the application automatically serves under that subfolder. See [Subfolder Deployment](subfolder-deployment.md).

Must **not** have a trailing slash.

<a id="app_public_avatar_url"></a>

### `app.public_avatar_url`

|             |                                                               |
| ----------- | ------------------------------------------------------------- |
| **Type**    | String (URL)                                                  |
| **Default** | `"https://avatar.example.com/user-avatars"` (must be changed) |

The public base URL where avatar files are accessible. This URL is used to build the avatar URL that is pushed to Authentik and LDAP. It must point to the location where the files from `avatar_storage_path` are served (either by the application itself or by a reverse proxy serving them directly).

Must **not** have a trailing slash.

<a id="app_web_session_lifetime_seconds"></a>

### `app.web_session_lifetime_seconds`

|             |                     |
| ----------- | ------------------- |
| **Type**    | Integer             |
| **Default** | `1800` (30 minutes) |

How long a login session lasts, in seconds. After this period the session cookie expires and the user must authenticate again via Authentik. The timer starts from the moment of login and is not extended by activity.

<a id="app_log_level"></a>

### `app.log_level`

|                  |                                                 |
| ---------------- | ----------------------------------------------- |
| **Type**         | String (enum)                                   |
| **Default**      | `"INFO"`                                        |
| **Valid values** | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

Controls the verbosity of log output:

| Level      | What gets logged                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------ |
| `DEBUG`    | Every action: OIDC flow, image resize steps, LDAP bind, API calls, HTTP requests, image metadata |
| `INFO`     | Key events: login, upload, save, API/LDAP updates                                                |
| `WARNING`  | Rejected uploads, missing TLS configuration, LDAP without SSL, LDAP cert verification disabled   |
| `ERROR`    | Failures in Authentik API or LDAP calls                                                          |
| `CRITICAL` | Fatal startup errors (missing or invalid config)                                                 |

<a id="app_debug_full"></a>

### `app.debug_full`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

Enables full debug mode. When active:
- Flask debugger is enabled (interactive traceback pages in the browser)
- Log level is forced to `DEBUG` regardless of `app.log_level`
- Template auto-reload is enabled (templates are re-read from disk on every request)

**Never enable in production.** A warning is logged at startup when this is active.

---

## Cleanup

<a id="cleanup_interval"></a>

### `cleanup.interval`

|             |                                  |
| ----------- | -------------------------------- |
| **Type**    | String (cron expression)         |
| **Default** | `"0 2 * * *"` (daily at 2:00 AM) |

Cron schedule for the cleanup job. Uses standard 5-field crontab syntax
(`minute hour day month weekday`). The schedule is evaluated in UTC.

The cleanup job runs four phases:

1. Remove avatar sets for deleted (and optionally deactivated) users
2. Enforce per-user retention (keep the N most recent uploads)
3. Remove size directories, format files, and image files that are no longer configured
4. Remove orphaned metadata files with no matching images on disk

Set to `""` (empty string) to disable the cleanup job entirely.

<a id="cleanup_on_startup"></a>

### `cleanup.on_startup`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, the cleanup job runs once 60 seconds after application startup, in addition to the regular cron schedule. Useful for catching up after extended downtime.

<a id="cleanup_avatar_retention_count"></a>

### `cleanup.avatar_retention_count`

|             |         |
| ----------- | ------- |
| **Type**    | Integer |
| **Default** | `2`     |

Number of avatar sets to keep per user. When a user has more than this many uploaded avatars, the cleanup job deletes the oldest ones. Set to `0` to keep all uploads indefinitely (no retention cleanup).

<a id="cleanup_when_user_deleted"></a>

### `cleanup.when_user_deleted`

|             |        |
| ----------- | ------ |
| **Type**    | Boolean |
| **Default** | `true` |

When enabled (default), the cleanup job removes all avatar sets belonging to users that have been deleted from Authentik entirely. A user is considered deleted when their PK no longer appears in any Authentik user listing.

Disable this setting only if you want to retain avatars indefinitely even for users that no longer exist in Authentik.

<a id="cleanup_when_user_deactivated"></a>

### `cleanup.when_user_deactivated`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, the cleanup job also removes avatar sets for users that exist in Authentik but are currently deactivated (`is_active=false`). Disabled by default so that avatars are preserved for accounts that may be re-enabled later.

Enable this setting if deactivated accounts should be treated the same as deleted ones for avatar storage purposes.

---

## Webserver

These settings apply when running via `run_app.py` / gunicorn (production and Docker). When running via `app.py` (development), only `host`, `port`, and TLS settings are used.

<a id="webserver_proxy_mode"></a>

### `webserver.proxy_mode`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (default), applies the `ProxyFix` middleware which reads `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix` headers set by a reverse proxy. This ensures `url_for()` generates correct external URLs and `remote_addr` reflects the real client IP.

Set to `false` only when running without a reverse proxy (direct exposure to the internet or local access only). When disabled, any forwarded headers sent by clients are ignored.

<a id="webserver_access_log"></a>

### `webserver.access_log`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, every HTTP request is logged to the console (except requests to `/static/` assets). Useful for debugging but verbose in production.

<a id="webserver_workers"></a>

### `webserver.workers`

|             |         |
| ----------- | ------- |
| **Type**    | Integer |
| **Default** | `2`     |

Number of gunicorn worker processes. Each worker is an independent OS process that handles requests. A reasonable starting point is `2 * CPU_cores + 1`, but for this application 2-4 workers are usually sufficient since image processing is the bottleneck, not concurrency.

<a id="webserver_threads"></a>

### `webserver.threads`

|             |         |
| ----------- | ------- |
| **Type**    | Integer |
| **Default** | `4`     |

Number of threads per worker. Each thread handles one request concurrently within a worker process. Threads help handle idle connections (e.g. SSE streams) without blocking the worker. The gunicorn worker class is `gthread` (threaded workers).

<a id="webserver_timeout"></a>

### `webserver.timeout`

|             |                   |
| ----------- | ----------------- |
| **Type**    | Integer (seconds) |
| **Default** | `120`             |

Worker timeout in seconds. A worker is restarted by gunicorn if it does not respond to the arbiter within this duration. Should be long enough for the slowest expected request (e.g. large image upload + processing + LDAP update). If you see `WORKER TIMEOUT` errors, increase this value.

<a id="webserver_tls_cert"></a>

### `webserver.tls_cert` / `tls_key`

|             |                      |
| ----------- | -------------------- |
| **Type**    | String (file path)   |
| **Default** | `""` (empty, no TLS) |

Paths to the TLS certificate and private key files for HTTPS. When both are set, the server uses HTTPS on the same port. When empty, the server runs over plain HTTP and a warning is logged at startup.

For production, terminate TLS at a reverse proxy instead. See [TLS Configuration](tls.md).

---

## OpenID Connect / Authentik Login

See [Authentik OIDC Setup](authentik-oidc-setup.md) for step-by-step setup instructions.

<a id="oidc_issuer_url"></a>

### `oidc.issuer_url`

|             |                                                                             |
| ----------- | --------------------------------------------------------------------------- |
| **Type**    | String (URL)                                                                |
| **Default** | `"https://auth.example.com/application/o/avatar-updater"` (must be changed) |

The Authentik OpenID provider URL. Follows the pattern `https://<authentik-domain>/application/o/<application-slug>`. The application appends `/.well-known/openid-configuration` to discover endpoints automatically.

Must **not** have a trailing slash.

<a id="oidc_client_id"></a>

### `oidc.client_id`

|             |                                      |
| ----------- | ------------------------------------ |
| **Type**    | String                               |
| **Default** | `"avatar-updater"` (must be changed) |

The OAuth2 client ID from the Authentik provider configuration.

<a id="oidc_client_secret"></a>

### `oidc.client_secret`

|             |                                 |
| ----------- | ------------------------------- |
| **Type**    | String                          |
| **Default** | `"CHANGE-ME"` (must be changed) |

The OAuth2 client secret from the Authentik provider configuration. Treat as a secret; do not commit to version control.

<a id="oidc_username_claim"></a>

### `oidc.username_claim`

|             |                        |
| ----------- | ---------------------- |
| **Type**    | String                 |
| **Default** | `"preferred_username"` |

The OIDC claim that carries the unique username. The value of this claim is used to look up the user via the Authentik API. In most Authentik setups, `preferred_username` is correct.

---

## Authentik Admin API

See [Authentik API Token](authentik-api-token.md) for step-by-step setup instructions.

<a id="authentik_api_base_url"></a>

### `authentik_api.base_url`

|             |                                                |
| ----------- | ---------------------------------------------- |
| **Type**    | String (URL)                                   |
| **Default** | `"https://auth.example.com"` (must be changed) |

The base URL of your Authentik instance (without trailing slash). The application appends API paths like `/api/v3/core/users/` to this URL.

<a id="authentik_api_api_token"></a>

### `authentik_api.api_token`

|             |                                 |
| ----------- | ------------------------------- |
| **Type**    | String                          |
| **Default** | `"CHANGE-ME"` (must be changed) |

An API token with permissions to read and write user attributes. See [Authentik API Token](authentik-api-token.md) for required permissions.

<a id="authentik_api_avatar_size"></a>

### `authentik_api.avatar_size`

|             |                  |
| ----------- | ---------------- |
| **Type**    | Integer (pixels) |
| **Default** | `1024`           |

Which generated image size (in pixels) to use for the avatar URL pushed to Authentik. This value **must** be one of the entries in `images.sizes`. The application validates this at startup and exits with an error if the size is not found.

<a id="authentik_api_avatar_attribute"></a>

### `authentik_api.avatar_attribute`

|             |                |
| ----------- | -------------- |
| **Type**    | String         |
| **Default** | `"avatar-url"` |

The Authentik user attribute name where the avatar URL is stored. The application sets `attributes.<this-value>` on the user object via the API. Change this if your Authentik configuration uses a different attribute name for avatar URLs.

---

## LDAP Server (optional)

Supports any standards-compliant LDAP server. Microsoft Active Directory is the primary and only tested target. See [MS AD Service Account](ms-ad-service-account.md) for setting up a least-privilege service account in Active Directory.

<a id="ldap_enabled"></a>

### `ldap.enabled`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

Set to `true` to enable LDAP thumbnail updates. When disabled, the entire LDAP module is a no-op and no LDAP connections are made.

<a id="ldap_server"></a>

### `ldap.server`

|             |                            |
| ----------- | -------------------------- |
| **Type**    | String                     |
| **Default** | `"ldaps://dc.example.com"` |

The LDAP server address. Use `ldaps://` for LDAP over TLS (recommended) or `ldap://` for plain LDAP.

<a id="ldap_port"></a>

### `ldap.port`

|             |         |
| ----------- | ------- |
| **Type**    | Integer |
| **Default** | `636`   |

The LDAP server port. Standard ports: `636` for LDAPS, `389` for LDAP.

<a id="ldap_use_ssl"></a>

### `ldap.use_ssl`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `true`  |

Whether to use SSL/TLS for the LDAP connection. Should be `true` when using port 636. A warning is logged at startup if LDAP is enabled without SSL.

<a id="ldap_skip_cert_verify"></a>

### `ldap.skip_cert_verify`

|             |         |
| ----------- | ------- |
| **Type**    | Boolean |
| **Default** | `false` |

Set to `true` to skip TLS certificate verification. Use only for self-signed certificates in development environments. A warning is logged at startup when this is enabled, as it makes the connection vulnerable to MITM attacks.

<a id="ldap_bind_dn"></a>

### `ldap.bind_dn`

|             |                                                         |
| ----------- | ------------------------------------------------------- |
| **Type**    | String                                                  |
| **Default** | `"CN=svc-avatar,OU=Service Accounts,DC=example,DC=com"` |

The distinguished name (DN) of the service account used to bind (authenticate) to the LDAP server. This account needs read access to search for users and write access to the photo attribute.

<a id="ldap_bind_password"></a>

### `ldap.bind_password`

|             |                                 |
| ----------- | ------------------------------- |
| **Type**    | String                          |
| **Default** | `"CHANGE-ME"` (must be changed) |

The password for the LDAP bind DN. Treat as a secret.

<a id="ldap_search_base"></a>

### `ldap.search_base`

|             |                       |
| ----------- | --------------------- |
| **Type**    | String                |
| **Default** | `"DC=example,DC=com"` |

The base DN under which user objects are searched. The search is performed with subtree scope, so users in any sub-OU are found.

<a id="ldap_search_filter"></a>

### `ldap.search_filter`

|             |                             |
| ----------- | --------------------------- |
| **Type**    | String                      |
| **Default** | `"(objectSid={ldap_uniq})"` |

The LDAP search filter used to locate the user object. The placeholder `{ldap_uniq}` is replaced with the user's `ldap_uniq` attribute value from Authentik (properly escaped for LDAP filter syntax).

The default uses `objectSid`, which is the standard unique identifier in Microsoft Active Directory. For other LDAP directories, change this to match your schema (e.g. `(uid={ldap_uniq})` for OpenLDAP).

<a id="ldap_photos"></a>

### `ldap.photos`

|          |                 |
| -------- | --------------- |
| **Type** | List of objects |

A list of LDAP attributes to update after each successful avatar upload. Each entry defines one attribute and how to populate it.

**Fields per entry:**

| Field           | Type    | Description                                                                                         |
| --------------- | ------- | --------------------------------------------------------------------------------------------------- |
| `attribute`     | String  | LDAP attribute name (e.g. `thumbnailPhoto`, `jpegPhoto`)                                            |
| `type`          | String  | `binary` (raw image bytes) or `url` (public URL string)                                             |
| `image_type`    | String  | Image format: `jpeg`, `png`, or `webp`                                                              |
| `image_size`    | Integer | Square pixel dimension (e.g. `96` = 96×96 px)                                                       |
| `max_file_size` | Integer | **Binary only.** Maximum size in KB. `0` = unlimited. Quality is reduced iteratively for JPEG/WebP. |

**Type `binary`:** Writes raw image bytes into the attribute. If a pre-generated file at the exact size and format already exists and fits within `max_file_size`, it is reused. Otherwise the image is generated on-the-fly from the closest equal-or-larger source and quality is reduced iteratively until the output fits.

**Type `url`:** Writes the public URL of a pre-generated image file as a string. Requires `image_size` to be present in `images.sizes` and `image_type` to be present in `images.formats`.

**Example:**

```yaml
photos:
  - attribute: thumbnailPhoto
    type: binary
    image_type: jpeg
    image_size: 96
    max_file_size: 100
  - attribute: jpegPhoto
    type: binary
    image_type: jpeg
    image_size: 648
    max_file_size: 0
```

---

## Image Processing

<a id="images_sizes"></a>

### `images.sizes`

|             |                                  |
| ----------- | -------------------------------- |
| **Type**    | List of integers                 |
| **Default** | `[1024, 648, 512, 256, 128, 64]` |

The square pixel dimensions to generate for each uploaded avatar. Every uploaded image is resized to each of these sizes. The value in `authentik_api.avatar_size` must appear in this list. LDAP photo entries with `type: url` also require their `image_size` to be in this list.

<a id="images_formats"></a>

### `images.formats`

|             |                          |
| ----------- | ------------------------ |
| **Type**    | List of strings          |
| **Default** | `["jpg", "png", "webp"]` |

The output formats to save for each size. Each size x format combination produces one file. Supported values: `jpg` (JPEG), `png`, `webp`.

<a id="images_jpeg_quality"></a>

### `images.jpeg_quality`

|             |                  |
| ----------- | ---------------- |
| **Type**    | Integer (1--100) |
| **Default** | `90`             |

JPEG compression quality. Higher values produce better quality but larger files. 90 is a good balance for avatars.

<a id="images_webp_quality"></a>

### `images.webp_quality`

|             |                  |
| ----------- | ---------------- |
| **Type**    | Integer (1--100) |
| **Default** | `85`             |

WebP compression quality. Similar to JPEG quality but WebP typically achieves better compression at the same visual quality.

<a id="images_png_compress_level"></a>

### `images.png_compress_level`

|             |                |
| ----------- | -------------- |
| **Type**    | Integer (0--9) |
| **Default** | `6`            |

PNG compression level. Higher values produce smaller files but take longer to compress. 6 is the default balance. PNG compression is lossless, so this only affects file size and compression speed, not image quality.
