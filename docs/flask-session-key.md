# Flask Session Key

The `app.secret_key` setting is used by Flask to cryptographically sign session cookies. If this key is predictable, too
short, or shared between environments, an attacker can forge session cookies and impersonate any user, including
administrators.

## Generating a key

Use one of these methods to generate a secure random key:

### Python (recommended)

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

This produces a 64-character hex string (256 bits of entropy).

### OpenSSL

```bash
openssl rand -hex 32
```

### PowerShell

```powershell
-join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 32) })
```

## Setting the key

Paste the generated value into `data/config/config.yml`:

```yaml
app:
  secret_key: "your-generated-key-here"
```

## Validation

The application validates the secret key at startup and **refuses to start** if:

- The key is still set to the default placeholder (`CHANGE-ME-to-a-random-secret-key`)
- The key is shorter than 32 characters

Both cases log a `FATAL` error with instructions on how to generate a proper key.

## Best practices

- **Never reuse** the key across environments (development, staging, production). Each environment should have its own
  unique key.
- **Never commit** the key to version control. The `config.yml` file should be excluded from your repository (it is
  already in `.gitignore`).
- **Rotate the key** if you suspect it may have been compromised. Rotating the key invalidates all existing sessions,
  forcing users to log in again.
- A minimum length of 32 characters (hex) is recommended. The generator commands above produce 64 characters by default.

See also: [Configuration Reference](configuration.md#appsecret_key)
