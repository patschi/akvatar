# Configuration Reference

All settings are defined in `data/config/config.yml`. Two example files are provided:

| File                         | Use when                                                    |
|------------------------------|-------------------------------------------------------------|
| `config.example-minimal.yml` | Getting started - only the required settings, short to read |
| `config.example-full.yml`    | Full reference - every option with inline comments          |

Copy whichever suits your situation:

```bash
# Minimal (fill in your URLs and secrets, everything else uses defaults)
cp data/config/config.example-minimal.yml data/config/config.yml

# Full (all options visible and commented)
cp data/config/config.example-full.yml data/config/config.yml
```

The application reads the configuration file once at startup. Changes require a restart to take
effect.

## Settings overview

| Setting                                                                          | Type    | Description                                         |
|----------------------------------------------------------------------------------|---------|-----------------------------------------------------|
| [`dry_run`](#dry_run)                                                            | Boolean | Skip Authentik/LDAP writes; log all actions instead |
| [`branding.name`](#brandingname)                                                 | String  | Application name shown in the UI                    |
| [`app.max_upload_size_mb`](#appmax_upload_size_mb)                               | Integer | Maximum upload size in MB                           |
| [`app.avatar_storage_path`](#appavatar_storage_path)                             | String  | Directory for stored avatar images                  |
| [`app.public_webui_url`](#apppublic_webui_url)                                   | URL     | Public URL where the application is reachable       |
| [`app.public_avatar_url`](#apppublic_avatar_url)                                 | URL     | Public URL where avatar files are served            |
| [`security.secret_key`](#securitysecret_key)                                     | String  | Flask session signing key                           |
| [`security.session_cookie_secure`](#securitysession_cookie_secure)               | Boolean | Override Secure flag on the session cookie          |
| [`security.web_session_lifetime_seconds`](#securityweb_session_lifetime_seconds) | Integer | Session cookie lifetime in seconds                  |
| [`security.metadata_access`](#securitymetadata_access)                           | Enum    | Who may access avatar metadata JSON files           |
| [`security.csp_enabled`](#securitycsp_enabled)                                   | Boolean | Send a Content-Security-Policy header on HTML pages |
| [`security.csp_report_only`](#securitycsp_report_only)                           | Boolean | CSP report-only mode: log violations, not enforced  |
| [`security.csp_report_uri`](#securitycsp_report_uri)                             | String  | URL where the browser sends CSP violation reports   |
| [`cleanup.interval`](#cleanupinterval)                                           | Cron    | Cron schedule for the cleanup job                   |
| [`cleanup.on_startup`](#cleanupon_startup)                                       | Boolean | Run cleanup once 60 s after startup                 |
| [`cleanup.avatar_retention_count`](#cleanupavatar_retention_count)               | Integer | Avatar sets to keep per user (0 = unlimited)        |
| [`cleanup.when_user_deleted`](#cleanupwhen_user_deleted)                         | Boolean | Remove avatars of users deleted from Authentik      |
| [`cleanup.when_user_deactivated`](#cleanupwhen_user_deactivated)                 | Boolean | Remove avatars of deactivated Authentik users       |
| [`rate_limiting.enabled`](#rate_limitingenabled)                                 | Boolean | Master switch for rate limiting                     |
| [`rate_limiting.ip_whitelist`](#rate_limitingip_whitelist)                       | List    | IPs/CIDRs exempt from rate limiting                 |
| [`rate_limiting.points_cost_404`](#rate_limitingpoints_cost_404)                 | Integer | Point cost for a 404 response                       |
| [`rate_limiting.eviction_interval`](#rate_limitingeviction_interval)             | Integer | Stale-entry cleanup interval in seconds             |
| [`rate_limiting.avatars`](#rate_limitingavatars)                                 | Object  | Rate limit settings for avatar image requests       |
| [`rate_limiting.metadata`](#rate_limitingmetadata)                               | Object  | Rate limit settings for metadata JSON requests      |
| [`rate_limiting.upload`](#rate_limitingupload)                                   | Object  | Per-user upload cooldown settings                   |
| [`rate_limiting.import_gravatar`](#rate_limitingimport_gravatar)                 | Object  | Per-user Gravatar import cooldown settings          |
| [`rate_limiting.import_url`](#rate_limitingimport_url)                           | Object  | Per-user URL import cooldown settings               |
| [`image_import.gravatar.enabled`](#image_importgravatarenabled)                  | Boolean | Enable Gravatar import in the UI                    |
| [`image_import.gravatar.restrict_email`](#image_importgravatarrestrict_email)    | Boolean | Lock Gravatar email to the session user's address   |
| [`image_import.url.enabled`](#image_importurlenabled)                            | Boolean | Enable URL import in the UI                         |
| [`image_import.url.restrict_private_ips`](#image_importurlrestrict_private_ips)  | Boolean | Block URLs resolving to private IP addresses        |
| [`image_import.webcam.enabled`](#image_importwebcamenabled)                      | Boolean | Enable browser webcam capture in the UI             |
| [`sentry.enabled`](#sentryenabled)                                               | Boolean | Master switch for Sentry error tracking             |
| [`sentry.dsn`](#sentrydsn)                                                       | String  | Sentry project DSN (ingest URL)                     |
| [`sentry.capture_errors`](#sentrycapture_errors)                                 | Boolean | Send unhandled exceptions to Sentry                 |
| [`sentry.capture_performance`](#sentrycapture_performance)                       | Boolean | Enable transaction / performance tracing            |
| [`sentry.sample_rate`](#sentrysample_rate)                                       | Float   | Error event sample rate (0.0-1.0)                   |
| [`sentry.traces_sample_rate`](#sentrytraces_sample_rate)                         | Float   | Performance trace sample rate (0.0-1.0)             |
| [`sentry.environment`](#sentryenvironment)                                       | String  | Sentry environment tag (auto-detected if empty)     |
| [`sentry.send_default_pii`](#sentrysend_default_pii)                             | Boolean | Include IP addresses and user details in events     |
| [`sentry.browser.enabled`](#sentrybrowserenabled)                                | Boolean | Master switch for browser-side Sentry               |
| [`sentry.browser.js_sdk_url`](#sentrybrowserjs_sdk_url)                          | String  | URL of the self-hosted Sentry JS SDK bundle         |
| [`sentry.browser.dsn`](#sentrybrowserdsn)                                        | String  | Browser-side Sentry DSN (falls back to sentry.dsn)  |
| [`sentry.browser.sample_rate`](#sentrybrowsersample_rate)                        | Float   | Browser error event sample rate (0.0-1.0)           |
| [`sentry.browser.traces_sample_rate`](#sentrybrowsertraces_sample_rate)          | Float   | Browser performance trace sample rate (0.0-1.0)     |
| [`sentry.browser.tunnel_enabled`](#sentrybrowsertunnel_enabled)                  | Boolean | Forward browser events via /api/sentry-event tunnel |
| [`app.log_level`](#applog_level)                                                 | Enum    | Log verbosity                                       |
| [`app.debug_full`](#appdebug_full)                                               | Boolean | Full debug mode - never enable in production        |
| [`webserver.proxy_mode`](#webserverproxy_mode)                                   | Boolean | Enable reverse-proxy header support (ProxyFix)      |
| [`webserver.trusted_hosts`](#webservertrusted_hosts)                             | List    | Restrict which Host header values are accepted      |
| [`webserver.access_log`](#webserveraccess_log)                                   | Boolean | Log every HTTP request to the console               |
| [`webserver.workers`](#webserverworkers)                                         | Integer | Number of gunicorn worker processes                 |
| [`webserver.threads`](#webserverthreads)                                         | Integer | Threads per worker                                  |
| [`webserver.timeout`](#webservertimeout)                                         | Integer | Worker timeout in seconds                           |
| [`webserver.tls.cert`](#webservertlscert--tlskey)                                | String  | Path to TLS certificate file                        |
| [`webserver.tls.key`](#webservertlscert--tlskey)                                 | String  | Path to TLS private key file                        |
| [`webserver.tls.min_version`](#webservertlsmin_version)                          | String  | Minimum TLS protocol version accepted by the server |
| [`webserver.http2.enabled`](#webserverhttp2enabled)                              | Boolean | Enable HTTP/2 via ALPN (requires TLS)               |
| [`oidc.issuer_url`](#oidcissuer_url)                                             | URL     | Authentik OIDC provider URL                         |
| [`oidc.client_id`](#oidcclient_id)                                               | String  | OAuth2 client ID                                    |
| [`oidc.client_secret`](#oidcclient_secret)                                       | String  | OAuth2 client secret                                |
| [`oidc.username_claim`](#oidcusername_claim)                                     | String  | OIDC claim used as the username                     |
| [`oidc.end_provider_session`](#oidcend_provider_session)                         | Boolean | End Authentik SSO session on logout                 |
| [`oidc.skip_cert_verify`](#oidcskip_cert_verify)                                 | Boolean | Skip TLS certificate verification for OIDC requests |
| [`authentik.base_url`](#authentikbase_url)                                       | URL     | Authentik instance base URL                         |
| [`authentik.api_token`](#authentikapi_token)                                     | String  | Authentik Admin API token                           |
| [`authentik.avatar_size`](#authentikavatar_size)                                 | Integer | Image size (px) used for the Authentik avatar URL   |
| [`authentik.avatar_format`](#authentikavatar_format)                             | String  | Image format used for the Authentik avatar URL      |
| [`authentik.avatar_attribute`](#authentikavatar_attribute)                       | String  | Authentik user attribute to store the avatar URL    |
| [`authentik.skip_cert_verify`](#authentikskip_cert_verify)                       | Boolean | Skip TLS certificate verification for API requests  |
| [`ldap.enabled`](#ldapenabled)                                                   | Boolean | Enable LDAP photo attribute updates                 |
| [`ldap.servers`](#ldapservers)                                                   | String  | LDAP server URL(s), comma-separated                 |
| [`ldap.port`](#ldapport)                                                         | Integer | LDAP server port (applied to all servers)           |
| [`ldap.use_ssl`](#ldapuse_ssl)                                                   | Boolean | Use SSL/TLS for the LDAP connection                 |
| [`ldap.skip_cert_verify`](#ldapskip_cert_verify)                                 | Boolean | Skip TLS certificate verification                   |
| [`ldap.bind_dn`](#ldapbind_dn)                                                   | String  | Service account DN for LDAP bind                    |
| [`ldap.bind_password`](#ldapbind_password)                                       | String  | Service account password                            |
| [`ldap.search_base`](#ldapsearch_base)                                           | String  | Base DN for user searches                           |
| [`ldap.search_filter`](#ldapsearch_filter)                                       | String  | LDAP filter to locate the user object               |
| [`ldap.photos`](#ldapphotos)                                                     | List    | LDAP photo attributes to update (see details below) |
| [`images.sizes`](#imagessizes)                                                   | List    | Square output sizes to generate (px)                |
| [`images.formats`](#imagesformats)                                               | List    | Output formats to save for each size                |
| [`images.jpeg_quality`](#imagesjpeg_quality)                                     | Integer | JPEG compression quality (1-100)                    |
| [`images.webp_quality`](#imageswebp_quality)                                     | Integer | WebP compression quality (1-100)                    |
| [`images.png_compress_level`](#imagespng_compress_level)                         | Integer | PNG compression level (0-9)                         |
| [`images.avif_quality`](#imagesavif_quality)                                     | Integer | AVIF compression quality (1-100)                    |
| [`images.rgba_background_color`](#imagesrgba_background_color)                   | List    | Background color used when converting RGBA to JPEG  |

---

## Dry-Run Mode

### `dry_run`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, avatar images are still processed and saved to disk, but no changes are pushed to
Authentik or LDAP. All operations that would have been performed are logged instead. Useful for
testing the full upload pipeline without affecting real user accounts.

---

## Branding

### `branding.name`

| Property    | Value              |
|-------------|--------------------|
| **Type**    | String             |
| **Default** | `"Avatar Updater"` |

The application name displayed in the browser title bar and the navigation header. Change this to
match your organization's branding (e.g. `"Contoso Avatar Updater"`).

---

## Application

### `app.max_upload_size_mb`

| Property    | Value   |
|-------------|---------|
| **Type**    | Integer |
| **Default** | `10`    |

Maximum allowed upload size in megabytes. Flask rejects files exceeding this limit before reaching
the upload handler. Note that the browser compresses images client-side before uploading, so
typical uploads are well under 1 MB regardless of this limit.

### `app.avatar_storage_path`

| Property    | Value                 |
|-------------|-----------------------|
| **Type**    | String (file path)    |
| **Default** | `"data/user-avatars"` |

Directory where processed avatar images and metadata are stored. Can be a relative path (relative
to the project root) or an absolute path. The application creates the directory and all required
subdirectories at startup if they do not exist.

### `app.public_webui_url`

| Property    | Value                                            |
|-------------|--------------------------------------------------|
| **Type**    | String (URL)                                     |
| **Default** | `"https://avatar.example.com"` (must be changed) |

The full public URL where users access the application. Used to generate OIDC redirect URIs and
other external links.

If the URL includes a path component (e.g. `https://portal.example.com/avatar`), the application
automatically serves under that subfolder. See [Subfolder Deployment](subfolder-deployment.md).

Must **not** have a trailing slash.

### `app.public_avatar_url`

| Property    | Value                                                         |
|-------------|---------------------------------------------------------------|
| **Type**    | String (URL)                                                  |
| **Default** | `"https://avatar.example.com/user-avatars"` (must be changed) |

The public base URL where avatar files are accessible. This URL is used to build the avatar URL
that is pushed to Authentik and LDAP. It must point to the location where the files from
`avatar_storage_path` are served (either by the application itself or by a reverse proxy serving
them directly).

Must **not** have a trailing slash.

### `app.log_level`

| Property         | Value                                           |
|------------------|-------------------------------------------------|
| **Type**         | String (enum)                                   |
| **Default**      | `"INFO"`                                        |
| **Valid values** | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

Controls the verbosity of log output:

| Level      | What gets logged                                                                                 |
|------------|--------------------------------------------------------------------------------------------------|
| `DEBUG`    | Every action: OIDC flow, image resize steps, LDAP bind, API calls, HTTP requests, image metadata |
| `INFO`     | Key events: login, upload, save, API/LDAP updates                                                |
| `WARNING`  | Rejected uploads, missing TLS configuration, LDAP without SSL, LDAP cert verification disabled   |
| `ERROR`    | Failures in Authentik API or LDAP calls                                                          |
| `CRITICAL` | Fatal startup errors (missing or invalid config)                                                 |

### `app.debug_full`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Enables full debug mode. When active:

- Flask debugger is enabled (interactive traceback pages in the browser)
- Log level is forced to `DEBUG` regardless of `app.log_level`
- Template auto-reload is enabled (templates are re-read from disk on every request)

**Never enable in production.** A warning is logged at startup when this is active.

---

## Security

### `security.secret_key`

| Property    | Value                                                               |
|-------------|---------------------------------------------------------------------|
| **Type**    | String                                                              |
| **Default** | `"CHANGE-ME-to-a-random-secret-key"` (placeholder, must be changed) |

The secret key used by Flask to cryptographically sign session cookies. If this key is predictable
or too short, an attacker can forge sessions.

The application **refuses to start** if:

- The key is still set to the default placeholder value
- The key is shorter than 32 characters

See [Flask Session Key](flask-session-key.md) for generation instructions.

### `security.session_cookie_secure`

| Property    | Value                                              |
|-------------|----------------------------------------------------|
| **Type**    | Boolean or `null`                                  |
| **Default** | `null` (auto-detected from `app.public_webui_url`) |

Controls whether the browser-side session cookie is marked with the `Secure` flag, which instructs
browsers to transmit the cookie only over HTTPS connections.

| Value   | Behavior                                                                                                           |
|---------|--------------------------------------------------------------------------------------------------------------------|
| `null`  | Auto-detect: `Secure` is set when `app.public_webui_url` starts with `https://` (correct for standard deployments) |
| `true`  | Always set `Secure`, regardless of `public_webui_url`                                                              |
| `false` | Never set `Secure` - only use this for plain-HTTP development environments                                         |

**You should not need to set this manually.** The auto-detection is correct for all standard
deployments, including reverse-proxy setups where TLS is terminated at the proxy and the internal
connection to Flask is plain HTTP. The `Secure` flag is enforced by the browser, not by the
Flask-to-proxy link.

### `security.web_session_lifetime_seconds`

| Property    | Value               |
|-------------|---------------------|
| **Type**    | Integer             |
| **Default** | `1800` (30 minutes) |

How long a login session lasts, in seconds. After this period the session cookie expires and the
user must authenticate again via Authentik. The timer starts from the moment of login and is not
extended by activity.

### `security.metadata_access`

| Property         | Value                  |
|------------------|------------------------|
| **Type**         | String (enum)          |
| **Default**      | `"owner_only"`         |
| **Valid values** | `owner_only`, `public` |

Controls who may read avatar metadata JSON files served at `/user-avatars/_metadata/<file>`.
Metadata files contain the user's Authentik PK (`user_pk`) alongside upload timestamps, sizes, and
formats. Exposing them to other users or the public could allow someone to enumerate user PKs if
they can observe or predict filenames.

| Value        | Behavior                                                                                                                                                                                                                                                                                           |
|--------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `owner_only` | Only the authenticated user who uploaded the file may access it. Unauthenticated visitors are redirected to the login page. Any other authenticated user - or a request for a file belonging to a different user - receives a `404` so the caller cannot distinguish "not found" from "not yours". |
| `public`     | No authentication is required. Any visitor may fetch any metadata file by its filename. Use this only when an unauthenticated external service needs direct access to metadata and you have accepted the privacy implications.                                                                     |

The default (`owner_only`) is recommended for all standard deployments.

### `security.csp_enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

Controls whether a `Content-Security-Policy` header is included on every HTML response.

When enabled, the policy:

- Allows scripts only from `'self'` and inline `<script>` tags that carry the per-request
  cryptographic nonce, blocking any injected scripts even if XSS is somehow achieved.
- Allows images from `'self'`, `data:` URIs (Cropper.js previews), `blob:` object URLs (Cropper.js
  canvas), and the origin extracted from `app.public_avatar_url`. This means avatar images served
  from a different domain or CDN are automatically permitted without manual configuration.
- Denies everything else by default (`default-src 'none'`), including frames
  (`frame-ancestors 'none'`).

Set to `false` only when a reverse proxy or WAF upstream of this application is responsible for
injecting its own CSP header and the two competing policies cause compatibility problems. **Do not
disable CSP without replacing it elsewhere.**

See [`security.csp_report_only`](#securitycsp_report_only) for testing a new policy without
enforcing it, and [`security.csp_report_uri`](#securitycsp_report_uri) for collecting browser
violation reports.

### `security.csp_report_only`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When `true`, the CSP header is sent as `Content-Security-Policy-Report-Only` instead of
`Content-Security-Policy`. In report-only mode the browser logs violations to the developer
console (or posts them to [`security.csp_report_uri`](#securitycsp_report_uri) when configured)
but does **not** block any resources. This is useful for testing a new or modified policy on a
live site without risking breakage.

When `false` (the default), the policy is enforced - any resource not explicitly permitted is
blocked.

Has no effect when [`security.csp_enabled`](#securitycsp_enabled) is `false`.

### `security.csp_report_uri`

| Property    | Value        |
|-------------|--------------|
| **Type**    | String (URL) |
| **Default** | `""` (empty) |

A URL where the browser sends JSON violation reports when a CSP rule is violated. When empty
(the default), violations are only logged to the browser's developer console and no report is
sent.

Only meaningful when [`security.csp_enabled`](#securitycsp_enabled) is `true`. Works in both
enforcing and report-only mode (see [`security.csp_report_only`](#securitycsp_report_only)).

---

## Cleanup

### `cleanup.interval`

| Property    | Value                            |
|-------------|----------------------------------|
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

### `cleanup.on_startup`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, the cleanup job runs once 60 seconds after application startup, in addition to the
regular cron schedule. Useful for catching up after extended downtime.

### `cleanup.avatar_retention_count`

| Property    | Value   |
|-------------|---------|
| **Type**    | Integer |
| **Default** | `2`     |

Number of avatar sets to keep per user. When a user has more than this many uploaded avatars, the
cleanup job deletes the oldest ones. Set to `0` to keep all uploads indefinitely (no retention
cleanup).

### `cleanup.when_user_deleted`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (default), the cleanup job removes all avatar sets belonging to users that have been
deleted from Authentik entirely. A user is considered deleted when their PK no longer appears in
any Authentik user listing.

Disable this setting only if you want to retain avatars indefinitely even for users that no longer
exist in Authentik.

### `cleanup.when_user_deactivated`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, the cleanup job also removes avatar sets for users that exist in Authentik but are
currently deactivated (`is_active=false`). Disabled by default so that avatars are preserved for
accounts that may be re-enabled later.

Enable this setting if deactivated accounts should be treated the same as deleted ones for avatar
storage purposes.

---

## Rate Limiting

Throttle avatar image and metadata JSON serving endpoints by client IP address to prevent
URL-guessing abuse and ensure fair usage. Additionally, per-user cooldowns can be applied to the
upload and image import proxy endpoints to prevent abuse.

The IP-based rate limits only affect the `/user-avatars/` endpoints - login, dashboard, static
files, and health checks are never rate-limited by IP.

Rate limiting counters are shared across all gunicorn worker processes, so the effective limit per
client IP is exactly `points` per `window` period regardless of how many workers are running. Each
request costs 1 point. A 404 response costs
[`points_cost_404`](#rate_limitingpoints_cost_404) points (default 5) to penalize URL-guessing
attempts.

Exceeding the limit returns HTTP 429 Too Many Requests with a `Retry-After` header and a JSON body:

```json
{
  "error": "Too Many Requests",
  "retry_after": 5
}
```

### `rate_limiting.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Master switch for rate limiting. When `false`, no rate limiting is applied and no background
threads are started.

### `rate_limiting.ip_whitelist`

| Property    | Value                  |
|-------------|------------------------|
| **Type**    | List of strings        |
| **Default** | `["127.0.0.1", "::1"]` |

IP addresses or CIDR ranges that are never rate-limited. Supports both individual IPs (e.g.
`10.0.0.1`) and CIDR notation (e.g. `192.168.0.0/16`). Both IPv4 and IPv6 are supported. Invalid
entries are logged as warnings and ignored.

### `rate_limiting.points_cost_404`

| Property    | Value   |
|-------------|---------|
| **Type**    | Integer |
| **Default** | `5`     |

Point cost charged for a 404 (Not Found) response on a rate-limited endpoint. A normal request
costs 1 point. Higher values penalize URL-guessing attempts more aggressively by consuming the
client's point budget faster.

### `rate_limiting.eviction_interval`

| Property    | Value             |
|-------------|-------------------|
| **Type**    | Integer (seconds) |
| **Default** | `10`              |

How often the central eviction thread prunes expired timestamps and removes stale tracking entries
from shared memory. Lower values unblock rate-limited clients sooner; higher values reduce IPC
overhead. The eviction thread runs once in the master process.

### `rate_limiting.avatars`

| Property | Value  |
|----------|--------|
| **Type** | Object |

Rate limit settings for avatar image requests (`/user-avatars/<dimensions>/<filename>`).

| Field     | Type    | Default | Description                                     |
|-----------|---------|---------|-------------------------------------------------|
| `enabled` | Boolean | `true`  | Enable rate limiting for this endpoint type     |
| `points`  | Integer | `100`   | Maximum points allowed per client IP per window |
| `window`  | Integer | `60`    | Time window in seconds                          |

### `rate_limiting.metadata`

| Property | Value  |
|----------|--------|
| **Type** | Object |

Rate limit settings for avatar metadata JSON requests (`/user-avatars/_metadata/<filename>`).

| Field     | Type    | Default | Description                                     |
|-----------|---------|---------|-------------------------------------------------|
| `enabled` | Boolean | `true`  | Enable rate limiting for this endpoint type     |
| `points`  | Integer | `50`    | Maximum points allowed per client IP per window |
| `window`  | Integer | `60`    | Time window in seconds                          |

### `rate_limiting.upload`

| Property | Value  |
|----------|--------|
| **Type** | Object |

Per-user cooldown for the avatar upload endpoint (`/api/upload`). Enforced across all gunicorn
worker processes via shared state. Requires the master switch
([`rate_limiting.enabled`](#rate_limitingenabled)) to be `true`.

| Field      | Type    | Default | Description                                          |
|------------|---------|---------|------------------------------------------------------|
| `enabled`  | Boolean | `true`  | Enable the per-user upload cooldown                  |
| `cooldown` | Integer | `10`    | Minimum seconds between consecutive uploads per user |

### `rate_limiting.import_gravatar`

| Property | Value  |
|----------|--------|
| **Type** | Object |

Per-user cooldown for the Gravatar import endpoint (`/api/fetch-gravatar`). Prevents abuse of the
import proxy as an outbound HTTP relay and limits load on the external Gravatar service. Requires the
master switch ([`rate_limiting.enabled`](#rate_limitingenabled)) to be `true`.

| Field      | Type    | Default | Description                                                   |
|------------|---------|---------|---------------------------------------------------------------|
| `enabled`  | Boolean | `true`  | Enable the per-user Gravatar import cooldown                  |
| `cooldown` | Integer | `3`     | Minimum seconds between consecutive Gravatar imports per user |

### `rate_limiting.import_url`

| Property | Value  |
|----------|--------|
| **Type** | Object |

Per-user cooldown for the URL import endpoint (`/api/fetch-url`). Prevents abuse of the import proxy
as an outbound HTTP relay. Requires the master switch
([`rate_limiting.enabled`](#rate_limitingenabled)) to be `true`.

| Field      | Type    | Default | Description                                              |
|------------|---------|---------|----------------------------------------------------------|
| `enabled`  | Boolean | `true`  | Enable the per-user URL import cooldown                  |
| `cooldown` | Integer | `3`     | Minimum seconds between consecutive URL imports per user |

---

## Sentry Error Tracking (optional)

Sends unhandled exceptions and (optionally) performance data to [Sentry](https://sentry.io). The
integration uses the official `sentry-sdk[flask]` package which auto-instruments Flask requests,
template rendering, and database calls.

When disabled (the default), `sentry-sdk` is never imported and adds zero runtime overhead.

### `sentry.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Master switch for the Sentry integration. Set to `true` and provide a valid [`dsn`](#sentry_dsn) to
start sending events. A warning is logged at startup if this is `true` but the DSN is empty.

### `sentry.dsn`

| Property    | Value        |
|-------------|--------------|
| **Type**    | String (URL) |
| **Default** | `""` (empty) |

The Sentry DSN (Data Source Name) for your project. Find it in
**Sentry - Project Settings - Client Keys (DSN)**. The DSN follows the format
`https://<key>@<org>.ingest.sentry.io/<project>`. Treat it as a secret - while it only allows
sending events (not reading them), exposing it allows anyone to submit events to your project.

### `sentry.capture_errors`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled, unhandled exceptions are captured and sent to Sentry as error events. Disabling this
sets `sample_rate` to `0.0` internally, which prevents any error events from being sent while still
allowing performance tracing if configured separately.

### `sentry.capture_performance`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, Flask request transactions are traced and sent to Sentry for performance monitoring.
Each traced request shows timing for the full request lifecycle including template rendering and
external API calls. Disabled by default because performance tracing produces significantly more
data than error tracking.

### `sentry.sample_rate`

| Property    | Value           |
|-------------|-----------------|
| **Type**    | Float (0.0-1.0) |
| **Default** | `1.0`           |

Fraction of error events to send. `1.0` sends every error, `0.5` sends roughly half, `0.0` sends
none. Only applies when [`capture_errors`](#sentry_capture_errors) is `true` - otherwise forced to
`0.0` regardless of this setting.

For most deployments, `1.0` is correct - you want to see every unhandled exception. Lower this only
if you have a high-traffic deployment generating excessive duplicate errors.

### `sentry.traces_sample_rate`

| Property    | Value           |
|-------------|-----------------|
| **Type**    | Float (0.0-1.0) |
| **Default** | `0.2`           |

Fraction of requests to trace for performance monitoring. `1.0` traces every request, `0.2` traces
roughly 20%. Only applies when [`capture_performance`](#sentry_capture_performance) is `true` -
otherwise forced to `0.0`.

Start with `0.2` and adjust based on your Sentry plan's event quota. Tracing every request (`1.0`)
provides the most complete picture but can quickly consume event budgets on busy instances.

### `sentry.environment`

| Property    | Value                                |
|-------------|--------------------------------------|
| **Type**    | String                               |
| **Default** | `""` (auto-detected from debug mode) |

The environment tag attached to every Sentry event. Used to filter events in the Sentry dashboard
(e.g. show only production errors).

When empty (the default), the environment is auto-detected:

| Condition                   | Resolved environment |
|-----------------------------|----------------------|
| `app.debug_full` is `true`  | `development`        |
| `app.debug_full` is `false` | `production`         |

Set explicitly to `"staging"`, `"testing"`, or any custom value if the auto-detection does not
match your setup.

### `sentry.send_default_pii`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Controls whether personally identifiable information (PII) is included in Sentry events. When
`false` (the default), Sentry automatically scrubs IP addresses, user agent strings, cookies, and
request bodies from events before they are stored.

Set to `true` only if your Sentry instance is self-hosted or your data processing agreement with
Sentry allows PII storage. This can be helpful for debugging user-specific issues but has privacy
implications.

### Browser-Side Sentry (optional)

Loads the Sentry JavaScript SDK in the browser so client-side errors and (optionally) performance
data are captured and forwarded to your Sentry project. The SDK bundle is served from your
self-hosted Sentry installation - no third-party CDN is involved.

**Sentry Tunnel:** By default, browser events are routed through a server-side tunnel endpoint
(`/api/sentry-event`) instead of being sent directly from the browser to the Sentry ingest host.
This has two advantages:

1. **CSP compatibility** - the tunnel is same-origin (`'self'`), so no Sentry-specific host needs
   to be added to the `connect-src` directive.
2. **Ad-blocker bypass** - browser-based ad blockers that block requests to known Sentry domains
   cannot intercept events sent to a first-party endpoint.

The tunnel validates that the DSN in each incoming envelope matches the configured browser DSN
before forwarding, so it cannot be abused as an open relay.

**DSN exposure:** The browser DSN is visible in the page source. Using a separate Sentry project
(and therefore a different DSN) for browser events is recommended so the server-side DSN is never
exposed. If both use the same DSN, only the write-only public key is revealed - reading events
still requires separate authentication.

### `sentry.browser.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Master switch for browser-side Sentry. Set to `true` and provide a valid
[`js_sdk_url`](#sentrybrowserjs_sdk_url) and [`dsn`](#sentrybrowserdsn) to start capturing
client-side events. When disabled (the default), no Sentry JavaScript is loaded and no tunnel
endpoint is registered.

### `sentry.browser.js_sdk_url`

| Property    | Value        |
|-------------|--------------|
| **Type**    | String (URL) |
| **Default** | `""` (empty) |

Full URL of the Sentry JavaScript SDK bundle served by your self-hosted Sentry installation.
Typically follows the pattern `https://sentry.example.com/js-sdk-loader/<loader-key>.min.js`.

The script is loaded via `<script src="...">` with the per-request CSP nonce. The **origin** of
this URL (scheme + host) is automatically added to the CSP `script-src` directive at startup, so
no manual CSP configuration is required.

A warning is logged at startup if browser Sentry is enabled but this value is empty.

### `sentry.browser.dsn`

| Property    | Value                             |
|-------------|-----------------------------------|
| **Type**    | String (URL)                      |
| **Default** | `""` (falls back to `sentry.dsn`) |

The Sentry DSN used by the browser SDK. When empty, the top-level [`sentry.dsn`](#sentrydsn) is
used as a fallback. A dedicated browser project DSN is recommended (see note above about DSN
exposure).

### `sentry.browser.sample_rate`

| Property    | Value           |
|-------------|-----------------|
| **Type**    | Float (0.0-1.0) |
| **Default** | `1.0`           |

Fraction of browser error events to send. `1.0` sends every error, `0.0` sends none. This
controls the JavaScript SDK's `sampleRate` option.

### `sentry.browser.traces_sample_rate`

| Property    | Value           |
|-------------|-----------------|
| **Type**    | Float (0.0-1.0) |
| **Default** | `0.2`           |

Fraction of browser page loads to trace for performance monitoring. `1.0` traces every page load,
`0.2` traces roughly 20%. This controls the JavaScript SDK's `tracesSampleRate` option.

### `sentry.browser.tunnel_enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (the default), the browser Sentry SDK sends envelopes to the server-side tunnel
endpoint (`/api/sentry-event`) instead of directly to the Sentry ingest host. The server validates
the envelope DSN and forwards the request to the real Sentry API.

Disable this only if you have added the Sentry ingest host to your CSP `connect-src` manually
(or CSP is disabled) and do not need ad-blocker bypass. When disabled, the SDK's `tunnel` option
is omitted and events are sent directly from the browser.

---

## Webserver

These settings apply when running via `run_app.py` / gunicorn (production and Docker). When running
via `app.py` (development), only `host`, `port`, and TLS settings are used.

### `webserver.proxy_mode`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (default), applies the `ProxyFix` middleware which reads `X-Forwarded-For`,
`X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix` headers set by a reverse proxy.
This ensures `url_for()` generates correct external URLs and `remote_addr` reflects the real client
IP.

Set to `false` only when running without a reverse proxy (direct exposure to the internet or local
access only). When disabled, any forwarded headers sent by clients are ignored.

### `webserver.trusted_hosts`

| Property    | Value                   |
|-------------|-------------------------|
| **Type**    | List of strings or null |
| **Default** | `null` (auto-derived)   |

A list of hostnames (and optional ports) that the application will accept in the HTTP `Host`
header. Any request whose `Host` value is not in this list is rejected with `HTTP 400 Bad Request`.

Protects against HTTP host-header injection attacks, where a malicious client or misconfigured
proxy sends a forged `Host` header, potentially poisoning URL generation (e.g. password-reset links
constructed with `url_for()`) or cache-poisoning downstream caches.

When set to `null` or omitted entirely (the default), the trusted hosts list is **automatically
derived** from the hostnames in [`app.public_webui_url`](#apppublic_webui_url) and
[`app.public_avatar_url`](#apppublic_avatar_url). This means the application only accepts `Host`
headers matching those configured URLs without any manual configuration. An empty list (`[]`) is
treated the same as `null`.

When set to an explicit list, only the listed hostnames are accepted and no auto-derivation takes
place.

Each entry is a plain hostname. Port is always stripped before the check, so the port number in the
`Host` header is never compared:

- `"avatar.example.com"` - matches `avatar.example.com` regardless of port.
- `".example.com"` - matches any subdomain of `example.com` (e.g. `avatar.example.com`,
  `api.example.com`).

```yaml
webserver:
  trusted_hosts:
    - "avatar.example.com"
    # - ".example.com"  # Wildcard: allow any subdomain of example.com
```

When `proxy_mode` is enabled (the default), `ProxyFix` has already resolved `X-Forwarded-Host`
into the request host before this check runs, so the list should contain the **public-facing**
hostname(s) as seen by clients, not internal hostnames.

### `webserver.access_log`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, every HTTP request is logged to the console (except requests to `/static/` assets).
Useful for debugging but verbose in production.

### `webserver.workers`

| Property    | Value   |
|-------------|---------|
| **Type**    | Integer |
| **Default** | `2`     |

Number of gunicorn worker processes. Each worker is an independent OS process that handles
requests. A reasonable starting point is `2 * CPU_cores + 1`, but for this application 2-4 workers
are usually sufficient since image processing is the bottleneck, not concurrency.

### `webserver.threads`

| Property    | Value   |
|-------------|---------|
| **Type**    | Integer |
| **Default** | `4`     |

Number of threads per worker. Each thread handles one request concurrently within a worker process.
Threads help handle idle connections (e.g., SSE streams) without blocking the worker. The gunicorn
worker class is `gthread` (threaded workers).

### `webserver.timeout`

| Property    | Value             |
|-------------|-------------------|
| **Type**    | Integer (seconds) |
| **Default** | `120`             |

Worker timeout in seconds. A worker is restarted by gunicorn if it does not respond to the arbiter
within this duration. Should be long enough for the slowest expected request (e.g., large image
upload + processing + LDAP update). If you see `WORKER TIMEOUT` errors, increase this value.

### `webserver.tls.cert` / `tls.key`

| Property    | Value                |
|-------------|----------------------|
| **Type**    | String (file path)   |
| **Default** | `""` (empty, no TLS) |

Paths to the TLS certificate and private key files for HTTPS. When both are set, the server uses
HTTPS on the same port. When empty, the server runs over plain HTTP and a warning is logged at
startup.

For production, terminate TLS at a reverse proxy instead. See [App TLS Configuration](app-tls.md).

### `webserver.tls.min_version`

| Property    | Value     |
|-------------|-----------|
| **Type**    | String    |
| **Default** | `TLSv1_2` |

Minimum TLS protocol version the server will accept from clients. The value must match a member name
of Python's `ssl.TLSVersion` enum. Supported values as of Python 3.7+:

| Value     | Protocol | Notes                                                    |
|-----------|----------|----------------------------------------------------------|
| `TLSv1_3` | TLS 1.3  | Most secure. Drops support for older clients.            |
| `TLSv1_2` | TLS 1.2  | Default. Broadly compatible and secure.                  |
| `TLSv1_1` | TLS 1.1  | Deprecated and disabled in most Python builds. Insecure. |
| `TLSv1`   | TLS 1.0  | Deprecated and disabled in most Python builds. Insecure. |

New TLS versions added to Python's `ssl` module are automatically recognized without requiring
application updates. If an unrecognized value is configured, the application refuses to start with
a FATAL error listing all valid values.

**Note:** Has no effect when `webserver.tls.cert` and `webserver.tls.key` are not set.

### `webserver.http2.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

Enable HTTP/2 support when TLS is configured. When `true` (default), gunicorn advertises the `h2`
protocol alongside `http/1.1` via the ALPN TLS extension, allowing HTTP/2-capable clients to
negotiate HTTP/2 automatically during the TLS handshake.

When `false`, ALPN is restricted to `http/1.1` only, even though the `h2` package is installed.
This gives you explicit control over which protocol is used.

**Requires:** `webserver.tls.cert` and `webserver.tls.key` must both be set. Has no effect when
running over plain HTTP - HTTP/2 negotiation only happens inside a TLS connection.

---

## OpenID Connect / Authentik Login

See [Authentik OIDC Setup](authentik-oidc-setup.md) for step-by-step setup instructions.

### `oidc.issuer_url`

| Property    | Value                                                                       |
|-------------|-----------------------------------------------------------------------------|
| **Type**    | String (URL)                                                                |
| **Default** | `"https://auth.example.com/application/o/avatar-updater"` (must be changed) |

The Authentik OpenID provider URL. Follows the pattern
`https://<authentik-domain>/application/o/<application-slug>`. The application appends
`/.well-known/openid-configuration` to discover endpoints automatically.

Must **not** have a trailing slash.

### `oidc.client_id`

| Property    | Value                                |
|-------------|--------------------------------------|
| **Type**    | String                               |
| **Default** | `"avatar-updater"` (must be changed) |

The OAuth2 client ID from the Authentik provider configuration.

### `oidc.client_secret`

| Property    | Value                           |
|-------------|---------------------------------|
| **Type**    | String                          |
| **Default** | `"CHANGE-ME"` (must be changed) |

The OAuth2 client secret from the Authentik provider configuration. Treat as a secret; do not
commit to version control.

### `oidc.username_claim`

| Property    | Value                  |
|-------------|------------------------|
| **Type**    | String                 |
| **Default** | `"preferred_username"` |

The OIDC claim that carries the unique username. The value of this claim is used to look up the
user via the Authentik API. In most Authentik setups, `preferred_username` is correct.

### `oidc.end_provider_session`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When enabled, logging out of the app also terminates the user's Authentik SSO session via
[RP-Initiated Logout](https://openid.net/specs/openid-connect-rpinitiated-1_0.html). This means the
user is logged out of **all applications** using that Authentik session, not just this app.

When disabled (default), only the local app session is cleared - the user remains logged into
Authentik and can immediately sign back in without re-entering credentials.

If you enable this, you must also register the post-logout redirect URI in your Authentik provider.
See [Post-Logout Redirect URI](authentik-oidc-setup.md#post-logout-redirect-uri) for details.

### `oidc.skip_cert_verify`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When `true`, TLS certificate verification is skipped for all OIDC requests - including the initial
discovery fetch (`/.well-known/openid-configuration`), the token exchange, and the userinfo
endpoint. This is useful when the Authentik instance uses a self-signed certificate and the
application communicates with it over an internal network.

A warning is logged at startup when this option is enabled. Do not enable this in production
environments where traffic may cross untrusted networks - disabling certificate verification
makes connections vulnerable to man-in-the-middle attacks.

---

## Authentik Admin API

See [Authentik API Token](authentik-api-token.md) for step-by-step setup instructions.

### `authentik.base_url`

| Property    | Value                                          |
|-------------|------------------------------------------------|
| **Type**    | String (URL)                                   |
| **Default** | `"https://auth.example.com"` (must be changed) |

The base URL of your Authentik instance (without trailing slash). The application appends API paths
like `/api/v3/core/users/` to this URL.

### `authentik.api_token`

| Property    | Value                           |
|-------------|---------------------------------|
| **Type**    | String                          |
| **Default** | `"CHANGE-ME"` (must be changed) |

An API token with permissions to read and write user attributes. See
[Authentik API Token](authentik-api-token.md) for required permissions.

### `authentik.avatar_size`

| Property    | Value            |
|-------------|------------------|
| **Type**    | Integer (pixels) |
| **Default** | `1024`           |

Which generated image size (in pixels) to use for the avatar URL pushed to Authentik. This value
**must** be one of the entries in `images.sizes`. The application validates this at startup and
exits with an error if the size is not found.

### `authentik.avatar_format`

| Property    | Value   |
|-------------|---------|
| **Type**    | String  |
| **Default** | `"jpg"` |

Which image format to use for the avatar URL pushed to Authentik. The value must be a recognized
format key and the resolved file extension must be present in `images.formats` (a file must
actually be generated for it). The application validates this at startup and exits with an error
if the format is not valid or not present in the generated formats list.

Supported values: `jpg` (or `jpeg`), `png`, `webp`, `avif`. Both `jpg` and `jpeg` are equivalent

- they resolve to the `.jpg` file extension. Using `avif` requires Pillow compiled with
  `libavif` support; see [`images.avif_quality`](#imagesavif_quality).

Use `jpg` for maximum compatibility. Use `png` for lossless encoding, `webp` for
high-efficiency encoding, or `avif` for modern clients where maximum compression matters.

### `authentik.avatar_attribute`

| Property    | Value      |
|-------------|------------|
| **Type**    | String     |
| **Default** | `"avatar"` |

The Authentik user attribute name where the avatar URL is stored. The application sets
`attributes.<this-value>` on the user object via the API. Change this if your Authentik
configuration uses a different attribute name for avatar URLs.

### `authentik.skip_cert_verify`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

When `true`, TLS certificate verification is skipped for all Authentik API requests. This is
useful when the Authentik instance uses a self-signed certificate and the application communicates
with it over an internal network.

A warning is logged at startup when this option is enabled. Do not enable this in production
environments where traffic may cross untrusted networks - disabling certificate verification
makes connections vulnerable to man-in-the-middle attacks.

---

## LDAP Server (optional)

Supports any standards-compliant LDAP server. Microsoft Active Directory is the primary and only
tested target. See [MS AD Service Account](ms-ad-service-account.md) for setting up a
least-privilege service account in Active Directory.

### `ldap.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Set to `true` to enable LDAP thumbnail updates. When disabled, the entire LDAP module is a no-op
and no LDAP connections are made.

### `ldap.servers`

| Property    | Value                                               |
|-------------|-----------------------------------------------------|
| **Type**    | String                                              |
| **Default** | `"ldaps://dc1.example.com,ldaps://dc2.example.com"` |

One or more LDAP server URLs, separated by commas. Servers are tried in the order listed; if a
connection or bind attempt fails (network unreachable, TLS handshake failure, timeout, etc.), the
next server is tried. This allows listing multiple domain controllers for automatic failover.

**Port and SSL per URL:** port and SSL/TLS mode can be specified directly in each URL and may
differ between servers. The URL scheme determines SSL (`ldaps://` - SSL on, `ldap://` - SSL off).
A port number in the URL takes precedence over [`ldap.port`](#ldapport). URLs without an explicit
scheme or port fall back to `ldap.use_ssl` and `ldap.port` respectively.

Example - single server:

```yaml
ldap:
  servers: "ldaps://dc.example.com"
```

Example - failover with matching protocol:

```yaml
ldap:
  servers: "ldaps://dc1.example.com,ldaps://dc2.example.com"
```

Example - per-URL port and mixed protocol:

```yaml
ldap:
  servers: "ldaps://dc1.example.com:636,ldap://dc2.example.com:389"
```

### `ldap.port`

| Property    | Value   |
|-------------|---------|
| **Type**    | Integer |
| **Default** | `636`   |

Fallback port used for any server in `ldap.servers` that does not include a port number in its URL.
Standard ports: `636` for LDAPS, `389` for LDAP.

### `ldap.use_ssl`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

Fallback SSL setting used for any server in `ldap.servers` whose URL does not have a recognized
scheme (`ldaps://` or `ldap://`). When the scheme is present in the URL it takes precedence over
this value. A warning is logged at startup if any server will connect without SSL.

### `ldap.skip_cert_verify`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `false` |

Set to `true` to skip TLS certificate verification. Use only for self-signed certificates in
development environments. A warning is logged at startup when this is enabled, as it makes the
connection vulnerable to MITM attacks.

### `ldap.bind_dn`

| Property    | Value                                                   |
|-------------|---------------------------------------------------------|
| **Type**    | String                                                  |
| **Default** | `"CN=svc-avatar,OU=Service Accounts,DC=example,DC=com"` |

The distinguished name (DN) of the service account used to bind (authenticate) to the LDAP server.
This account needs read access to search for users and write access to the photo attribute.

### `ldap.bind_password`

| Property    | Value                           |
|-------------|---------------------------------|
| **Type**    | String                          |
| **Default** | `"CHANGE-ME"` (must be changed) |

The password for the LDAP bind DN. Treat as a secret.

### `ldap.search_base`

| Property    | Value                 |
|-------------|-----------------------|
| **Type**    | String                |
| **Default** | `"DC=example,DC=com"` |

The base DN under which user objects are searched. The search is performed with subtree scope, so
users in any sub-OU are found.

### `ldap.search_filter`

| Property    | Value                       |
|-------------|-----------------------------|
| **Type**    | String                      |
| **Default** | `"(objectSid={ldap_uniq})"` |

The LDAP search filter used to locate the user object. The placeholder `{ldap_uniq}` is replaced
with the user's `ldap_uniq` attribute value from Authentik (properly escaped for LDAP filter
syntax).

The default uses `objectSid`, which is the standard unique identifier in Microsoft Active
Directory. For other LDAP directories, change this to match your schema (e.g.
`(uid={ldap_uniq})` for OpenLDAP).

### `ldap.photos`

| Property | Value           |
|----------|-----------------|
| **Type** | List of objects |

A list of LDAP attributes to update after each successful avatar upload. Each entry defines one
attribute and how to populate it.

**Fields per entry:**

| Field           | Type    | Description                                                                                         |
|-----------------|---------|-----------------------------------------------------------------------------------------------------|
| `attribute`     | String  | LDAP attribute name (e.g. `thumbnailPhoto`, `jpegPhoto`)                                            |
| `type`          | String  | `binary` (raw image bytes) or `url` (public URL string)                                             |
| `image_type`    | String  | Image format: `jpeg`, `png`, `webp`, or `avif`                                                      |
| `image_size`    | Integer | Square pixel dimension (e.g. `96` = 96×96 px)                                                       |
| `max_file_size` | Integer | **Binary only.** Maximum size in KB. `0` = unlimited. Quality is reduced iteratively for JPEG/WebP. |

**Type `binary`:** Writes raw image bytes into the attribute. If a pre-generated file at the exact
size and format already exists and fits within `max_file_size`, it is reused. Otherwise the image
is generated on-the-fly from the closest equal-or-larger source and quality is reduced iteratively
until the output fits.

**Type `url`:** Writes the public URL of a pre-generated image file as a string. Requires
`image_size` to be present in `images.sizes` and `image_type` to be present in `images.formats`.

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

## Image Import (External Sources)

Controls whether users can import images from external sources (Gravatar by email, a remote URL, or
a live webcam capture) instead of uploading a file directly. Gravatar and URL import are enabled by
default; webcam capture is opt-in because it requires a secure origin and adjusts the
`Permissions-Policy` response header. When a method is disabled, its trigger button is hidden from
the dashboard and the corresponding server endpoint (where applicable) returns HTTP 403.

### `image_import.gravatar.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (default), users can import their avatar from [Gravatar](https://gravatar.com) by
entering an email address. The server fetches the image from Gravatar's API and proxies it back to
the browser. Set to `false` to hide the Gravatar import option from the UI entirely.

### `image_import.gravatar.restrict_email`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When set to `true`, the Gravatar email input is replaced by a read-only display of the
authenticated user's registered email address. Users cannot enter a different address - the dialog
simply shows their account email as static text.

The server enforces the same restriction on every request: the submitted email must exactly match
the session user's email, and any mismatch is rejected with HTTP 403. This dual enforcement
(UI lock + server validation) prevents the Gravatar proxy endpoint from being used as an
account-existence oracle for arbitrary email addresses.

Enable this option when you want to ensure users can only import their own Gravatar and cannot
probe whether other email addresses have a Gravatar registered.

### `image_import.url.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (default), users can import an image from any HTTP/HTTPS URL. The server fetches the
image and proxies it back to the browser. Set to `false` to hide the URL import option from the UI
entirely.

### `image_import.url.restrict_private_ips`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled (default), the server resolves the hostname of a user-provided URL before fetching
and blocks requests that resolve to private, loopback, link-local, or otherwise
non-globally-routable IP addresses. This prevents
[Server-Side Request Forgery (SSRF)](https://owasp.org/www-community/attacks/Server-Side_Request_Forgery)
attacks where a user could use the import feature to probe or access internal services.

Blocked ranges include `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8` (loopback),
`169.254.0.0/16` (link-local), and their IPv6 equivalents.

Set to `false` only if your deployment requires fetching images from internal network hosts (not
recommended in production).

### `image_import.webcam.enabled`

| Property    | Value   |
|-------------|---------|
| **Type**    | Boolean |
| **Default** | `true`  |

When enabled, a **Webcam** tab is shown in the import dialog that lets users capture a selfie using
their device's camera via the browser's
[`MediaDevices.getUserMedia()`](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)
API. Capture happens entirely client-side - the frame is drawn to an offscreen canvas, converted to
a JPEG blob, and handed to the same crop/upload pipeline as a locally selected file. No image data
is sent to the server until the user confirms the capture and clicks **Upload**.

Enabling this flag flips the `Permissions-Policy` response header from `camera=()` to
`camera=(self)`. Without this change, browsers hard-block `getUserMedia()` regardless of user
consent. The feature only works on secure origins, so the app must be served over HTTPS (or
accessed via `http://localhost` during development).

---

## Image Processing

### `images.sizes`

| Property    | Value                            |
|-------------|----------------------------------|
| **Type**    | List of integers                 |
| **Default** | `[1024, 648, 512, 256, 128, 64]` |

The square pixel dimensions to generate for each uploaded avatar. Every uploaded image is resized
to each of these sizes. The value in `authentik.avatar_size` must appear in this list. LDAP photo
entries with `type: url` also require their `image_size` to be in this list.

### `images.formats`

| Property    | Value                    |
|-------------|--------------------------|
| **Type**    | List of strings          |
| **Default** | `["jpg", "png", "webp"]` |

The output formats to save for each size. Each size x format combination produces one file.
Supported values: `jpg` (JPEG), `png`, `webp`, `avif`. Each entry is validated against the
known format list at startup - an unrecognized value (e.g. `bmp`, `gif`) causes a FATAL error
and prevents the application from starting. Adding `avif` additionally requires Pillow compiled
with `libavif` support; a startup warning is logged if the codec is unavailable.

### `images.jpeg_quality`

| Property    | Value            |
|-------------|------------------|
| **Type**    | Integer (1--100) |
| **Default** | `90`             |

JPEG compression quality. Higher values produce better quality but larger files. 90 is a good
balance for avatars.

### `images.webp_quality`

| Property    | Value            |
|-------------|------------------|
| **Type**    | Integer (1--100) |
| **Default** | `85`             |

WebP compression quality. Similar to JPEG quality but WebP typically achieves better compression at
the same visual quality.

### `images.png_compress_level`

| Property    | Value          |
|-------------|----------------|
| **Type**    | Integer (0--9) |
| **Default** | `6`            |

PNG compression level. Higher values produce smaller files but take longer to compress. 6 is the
default balance. PNG compression is lossless, so this only affects file size and compression speed,
not image quality.

### `images.avif_quality`

| Property    | Value            |
|-------------|------------------|
| **Type**    | Integer (1--100) |
| **Default** | `80`             |

AVIF compression quality. Higher values produce better quality but larger files. Only used when
`avif` is included in [`images.formats`](#imagesformats).

**Requires:** Pillow compiled with `libavif` support (available in Pillow 9.1+ when the system
`libavif` library is present). The application logs a warning at startup if AVIF is listed in
`images.formats` but the codec is unavailable, so the operator gets an early signal instead of a
cryptic runtime error on the first upload.

### `images.rgba_background_color`

| Property    | Value             |
|-------------|-------------------|
| **Type**    | List of integers  |
| **Default** | `[255, 255, 255]` |

The solid RGB background color used when compositing RGBA images (those with an alpha channel)
before encoding to JPEG or non-transparent WebP. Without compositing, transparent and
semi-transparent pixels would appear black in the output; compositing onto this color produces
the intended result for logos and images with transparent borders.

Each element is an integer in the range `0-255` representing the Red, Green, and Blue channels
respectively. The default `[255, 255, 255]` is white.

Examples:

- `[255, 255, 255]` - white (default)
- `[0, 0, 0]` - black
- `[128, 128, 128]` - medium grey
