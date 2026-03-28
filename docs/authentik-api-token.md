# Authentik API Token

The Avatar Updater uses the Authentik Admin API to read user attributes and set the avatar URL on the user object. This requires an API token with the appropriate permissions.

## What the application does with the API

1. **Resolve user PK:** At login time, the app calls `GET /api/v3/core/users/?username=<username>` to find the user's numeric primary key (PK). This PK is used as a stable identifier for all subsequent operations.
2. **Read user attributes:** After uploading an avatar, the app calls `GET /api/v3/core/users/<pk>/` to fetch the user's current `attributes` dict. This is needed to read the `ldap_uniq` value (used for LDAP updates) without overwriting other custom attributes.
3. **Write avatar URL:** The app calls `PATCH /api/v3/core/users/<pk>/` to set `attributes.avatar-url` (or whichever attribute is configured via `authentik_api.avatar_attribute`) to the public URL of the uploaded avatar.
4. **List active users:** The cleanup job calls `GET /api/v3/core/users/?is_active=true` to determine which users still exist, so it can remove avatars of deleted or deactivated users.

## Create the token

1. In the Authentik admin panel, go to **Directory > Tokens and App passwords**
2. Click **Create**
3. Fill in:
   - **Identifier**: e.g. `avatar-updater-api`
   - **Intent**: select **API Token**
   - **User**: assign it to a user that has permissions to read and write user attributes (see [Required permissions](#required-permissions) below)
   - **Expiring**: decide based on your security policy. A non-expiring token is simpler but requires manual rotation.
4. Click **Create** and copy the token value

## Fill in the config

Paste the token into `data/config/config.yml`:

```yaml
authentik_api:
  base_url: "https://your-authentik-domain"
  api_token: "<token-from-above>"
  avatar_size: 1024
  avatar_attribute: "avatar-url"
```

See [Configuration Reference](configuration.md#authentik-admin-api) for details on each setting.

## Required permissions

The token's owning user needs the following permissions:

| Permission | Why |
|---|---|
| **Read** on `User` objects | Resolve usernames to PKs, read attributes (including `ldap_uniq`), list active users for cleanup |
| **Write** on `User.attributes` | Set the avatar URL attribute on user objects |

The simplest approach is to assign the token to an **admin** user. For a least-privilege setup, create a dedicated service account in Authentik and grant it only the permissions listed above through Authentik's RBAC system.

## Security considerations

- **Treat the token as a secret.** It grants write access to user attributes. Do not commit it to version control.
- **Scope the token to the minimum required permissions.** If your Authentik version supports fine-grained API permissions, restrict the token to user attribute reads and writes only.
- **Rotate the token periodically** if your security policy requires it. After rotating, update `authentik_api.api_token` in `config.yml` and restart the application.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| Login fails with `?error=pk_failed` | The API token cannot read user objects | Verify the token is valid and the owning user has read permissions |
| Avatar upload succeeds but Authentik avatar does not update | The API token cannot write user attributes | Verify the owning user has write permissions on user attributes |
| Cleanup job logs "Failed to fetch active users from Authentik" | Token expired or API unreachable | Check token validity and network connectivity to Authentik |
