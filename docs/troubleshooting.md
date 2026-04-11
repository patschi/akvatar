# Troubleshooting

General tips, common issues, and known problems with their solutions.

## General tips

### Enable debug logging

Set [`app.log_level`](configuration.md#applog_level) to `DEBUG` in `config.yml` and restart the application. This logs
every OIDC step, API call, image processing action, and HTTP request — usually enough to pinpoint the problem.

```yaml
app:
  log_level: "DEBUG"
```

### Check the container logs

```bash
# Docker Compose
docker compose logs -f akvatar

# Plain Docker
docker logs -f akvatar
```

### Verify the configuration file

The application reads `data/config/config.yml` once at startup. After any change you must restart the container or
process. Compare your file against [`config.example-full.yml`](../data/config/config.example-full.yml) to spot typos or
missing keys.

### Test in a private browser window

When login or session problems occur, try an **incognito / private browsing window** first. This rules out stale
cookies, cached redirects, or browser extensions interfering with the flow.

### Inspect browser developer tools

Open the browser's Network tab before clicking **Sign in**. Watch for:

- **`Set-Cookie` header** in the response from `GET /login-start` — confirms the session cookie is being set.
- **`Cookie` header** in the request to `GET /callback` — confirms the browser is sending the session cookie back.
- **HTTP status codes** on each redirect — `302` is expected for the OIDC flow; `500` indicates a server-side error.

### Check reverse proxy headers

When running behind a reverse proxy, ensure these headers are forwarded correctly:

- `X-Forwarded-For`
- `X-Forwarded-Proto`
- `X-Forwarded-Host`
- `X-Forwarded-Prefix` (only needed for [subfolder deployments](subfolder-deployment.md))

Incorrect or missing headers can cause redirect URI mismatches, wrong URL generation, or session cookie problems.

---

## Known issues

### OIDC login fails: `MismatchingStateError`

**Error:**

```text
authlib.integrations.base_client.errors.MismatchingStateError:
mismatching_state: CSRF Warning! State not equal in request and response.
```

**What it means:** The OIDC `state` parameter stored in the session cookie during `/login-start` does not match the one
received on `/callback`. This almost always means the session cookie was lost or overwritten between the two requests.

**Common causes and fixes:**

| Cause                                                                                                                                                                                                                                                                                                                                        | Fix                                                                                                                                                                                                                                  |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **`Secure` cookie flag vs HTTP access.** When [`app.public_base_url`](configuration.md#apppublic_base_url) starts with `https://`, the session cookie is automatically marked `Secure`, and browsers will not send it over plain HTTP. If you access the app via `http://` (e.g. during local development), the cookie is silently dropped. | Either access the app over HTTPS, or set [`app.session_cookie_secure`](configuration.md#app_session_cookie_secure) to `false` in `config.yml`.                                                                                       |
| **Cookie name collision on a shared domain.** If the application and Authentik share the same domain (e.g. both on `portal.example.com`), another application's session cookie could overwrite the one set by this application.                                                                                                              | This is mitigated by default: the application uses `akvatar_session` as its cookie name to avoid collisions with common `session` cookie names. If you still suspect a collision, check the browser's cookie storage for the domain. |
| **Stale cookies from a previous deployment.** A crashed or misconfigured deployment may have left behind unusable session cookies.                                                                                                                                                                                                           | Clear browser cookies for the application's domain, or test in a private window.                                                                                                                                                     |
| **Reverse proxy stripping cookies.** Some proxy configurations strip or rewrite `Set-Cookie` headers.                                                                                                                                                                                                                                        | Verify the proxy passes `Set-Cookie` and `Cookie` headers through unchanged. Check with the browser Network tab.                                                                                                                     |

### Uploaded image not visible in Authentik

**Symptoms:** Upload completes successfully (all steps green), but the user's avatar in Authentik still shows the old
image or a default placeholder.

**Things to check:**

1. **Authentik avatar source setting.** In the Authentik admin panel, ensure the user's avatar source is set to use the
   attribute configured in [`authentik.avatar_attribute`](configuration.md#authentikavatar_attribute) (default:
   `avatar-url`). Authentik may be using a different source (Gravatar, initials) that takes precedence.
2. **Browser cache.** Hard-refresh (`Ctrl+Shift+R` / `Cmd+Shift+R`) the Authentik page to bypass cached images.
3. **Public avatar URL.** Verify that [`app.public_avatar_url`](configuration.md#apppublic_avatar_url) points to a URL
   that is actually reachable from the browser. If the URL is only reachable from the server (e.g., an internal Docker
   network address), browsers cannot load the image.
4. **Dry-run mode.** When [`dry_run`](configuration.md#dry_run) is `true`, images are processed and saved to disk, but
   the Authentik API call is skipped. Check the logs for `[DRY-RUN]` entries.

### Login page shows "Your session has expired"

**What it means:** While the dashboard was open, the server-side session expired (default timeout is 30 minutes of
inactivity). The dashboard's background session-liveness check (`/api/session`, polled every 60 s) detected the
expiry and redirected to the login page with `?error=session_expired`.

**Fix:** This is expected behaviour — simply sign in again. If sessions expire faster than expected, check
[`app.session_lifetime`](configuration.md#appweb_session_lifetime_seconds) in `config.yml`.

---

### Cleanup removes nothing or skips unexpectedly

**Symptoms:** The cleanup job runs but logs `nothing to remove` even though you expect files to be cleaned up.

**Things to check:**

1. **Safety guard.** If the Authentik API returns zero users (e.g., expired API token, network error), cleanup aborts
   entirely to prevent accidental mass deletion. Check the logs for `Authentik returned zero users`.
2. **Cleanup flags.** By default, only avatars of *deleted* users are removed. Deactivated users are preserved unless 
   [`cleanup.when_user_deactivated`](configuration.md#cleanupwhen_user_deactivated) is set to `true`.
3. **Retention count.** [`cleanup.avatar_retention_count`](configuration.md#cleanupavatar_retention_count) defaults to
   `2`. Users with two or fewer avatar sets will not have any removed by retention cleanup.
4. **Dry-run mode.** When [`dry_run`](configuration.md#dry_run) is `true`, cleanup logs what *would* be removed but does
   not delete anything.
