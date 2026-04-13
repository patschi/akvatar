# Authentik OIDC Setup

This guide walks through creating the OAuth2/OpenID Connect provider and application in Authentik that the Avatar
Updater needs for user authentication.

## Overview

The Avatar Updater uses the **OpenID Connect Authorization Code Flow** to authenticate users. This requires:

1. A **signing key** for ID tokens
2. An **OAuth2/OpenID Provider** configured in Authentik
3. An **Application** linked to that provider

## 1. Create a Signing Key

If you already have a signing key for OIDC, skip this step.

1. In the Authentik admin panel, go to **System > Certificates**
2. Click **Generate** to create a new self-signed certificate/key pair
3. Give it a name (e.g. `OIDC Signing Key`) and save
4. This key is used by the OIDC provider to sign ID tokens; the Avatar Updater validates the signature during login

## 2. Create an OAuth2/OpenID Provider

1. Go to **Applications > Providers** and click **Create**
2. Select **OAuth2/OpenID Provider**
3. Fill in:
    - **Name**: e.g. `Avatar Updater`
    - **Authorization flow**: select your standard authorization flow
    - **Client ID**: note the auto-generated value (or set your own); goes into `oidc.client_id`
      in [config.yml](configuration.md#oidc_client_id)
    - **Client Secret**: note the auto-generated value; goes into `oidc.client_secret`
      in [config.yml](configuration.md#oidc_client_secret)
    - **Redirect URIs/Origins**: set to `https://your-app-url/callback` (the `/callback` path is required and must match
      exactly)
    - **Signing Key**: select the certificate you created in step 1
4. Under **Advanced protocol settings**:
    - **Scopes**: ensure `openid`, `profile`, and `email` are selected (the app hardcodes these three scopes)
    - **Subject mode**: can be left at the default ("Based on the hashed User ID"); the app looks up users by username,
      not by `sub` claim
5. Save the provider

## 3. Create an Application

1. Go to **Applications > Applications** and click **Create**
2. Fill in:
    - **Name**: e.g. `Avatar Updater`
    - **Slug**: e.g. `avatar-updater` (this slug is part of the issuer URL)
    - **Provider**: select the OAuth2/OpenID Provider you just created
3. Save the application

## 4. Fill in the config

Using the values from above, fill in `data/config/config.yml`:

```yaml
oidc:
  issuer_url: "https://your-authentik-domain/application/o/avatar-updater"
  client_id: "<client-id-from-step-2>"
  client_secret: "<client-secret-from-step-2>"
  username_claim: "preferred_username"
```

The `issuer_url` follows the pattern `https://<authentik-domain>/application/o/<application-slug>`.

## Redirect URI

The OIDC callback endpoint is `/callback`. The full redirect URI depends on how the app
is deployed:

```text
  Root domain:    https://avatar.example.com/callback
                  └────────────────────────┘└────────┘
                        public_webui_url       fixed path

  Subfolder:      https://portal.example.com/avatar/callback
                  └───────────────────────────────┘└────────┘
                             public_webui_url        fixed path
```

| Deployment  | Redirect URI                                 |
|-------------|----------------------------------------------|
| Root domain | `https://avatar.example.com/callback`        |
| Subfolder   | `https://portal.example.com/avatar/callback` |

This URI must match **exactly** what is configured in the Authentik provider's "Redirect URIs/Origins" field. A mismatch
causes a "mismatching redirection URI" error during login.

See also: [Subfolder Deployment](subfolder-deployment.md) for subfolder-specific considerations.

## Post-Logout Redirect URI

> **Note:** This section only applies when [`oidc.end_provider_session`](configuration.md#oidcend_provider_session)
> is set to `true`. By default, logging out only clears the local app session and this step is not needed.

When `oidc.end_provider_session` is enabled, logging out performs **RP-Initiated Logout**: the app clears the local
session and redirects the browser to Authentik's `end_session_endpoint` so the SSO session is terminated as well. This
logs the user out of **all applications** using that Authentik session. Authentik then redirects the user back to the
app's `/logged-out` page.

For this redirect to work, the post-logout URI must be registered in the Authentik provider's
"Redirect URIs/Origins" field alongside the login callback URI:

```text
  Root domain:    https://avatar.example.com/logged-out
  Subfolder:      https://portal.example.com/avatar/logged-out
```

| Deployment  | Post-Logout Redirect URI                       |
|-------------|------------------------------------------------|
| Root domain | `https://avatar.example.com/logged-out`        |
| Subfolder   | `https://portal.example.com/avatar/logged-out` |

If the post-logout URI is not registered, Authentik will still end the SSO session but may show its own
generic logged-out page instead of redirecting back to the app.

## OIDC claims used

The app reads the following claims from the ID token / userinfo response:

| Claim                                                 | Purpose                                                | Required                                                  |
|-------------------------------------------------------|--------------------------------------------------------|-----------------------------------------------------------|
| `preferred_username` (or configured `username_claim`) | Identifies the user for Authentik API lookups          | Yes                                                       |
| `name`                                                | Display name shown on the dashboard                    | No (falls back to username)                               |
| `email`                                               | Shown on the dashboard                                 | No                                                        |
| `locale`                                              | Used to select the UI language (e.g. `en_US`, `de_DE`) | No (falls back to `Accept-Language` header, then English) |

## Troubleshooting

| Problem                                        | Cause                                                                  | Fix                                                                                |
|------------------------------------------------|------------------------------------------------------------------------|------------------------------------------------------------------------------------|
| "mismatching redirection URI"                  | Redirect URI in Authentik does not match the app's callback URL        | Ensure the URI in the provider matches `<public_webui_url>/callback` exactly       |
| Login redirects back with `?error=oidc_failed` | Token exchange failed (network error, invalid secret, expired code)    | Check the app logs for the full exception; verify `client_secret` matches          |
| Login redirects back with `?error=pk_failed`   | Authentik API could not resolve the user's primary key                 | Ensure the [API token](authentik-api-token.md) has permission to read users        |
| Login page shows "session expired" message     | Dashboard detected an expired server-side session via `/api/heartbeat` | Normal behavior - the user was idle past the session timeout; simply sign in again |

See also: [Configuration Reference](configuration.md#openid-connect--authentik-login)
