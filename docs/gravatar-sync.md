# Gravatar Sync (manual script)

`run_sync_gravatar.py` is a manual, one-time job that pulls the email address of
each Authentik user, fetches their [Gravatar](https://gravatar.com) image, and
imports it through the **same processing pipeline as a manual upload** (server-side
resize to every configured size and format, Authentik attribute write, optional
LDAP write, metadata, and optional webhooks). No cropping is applied - Gravatar
serves square images.

Re-running the script picks up changes: if a user's Gravatar image was updated, the
new image is re-imported. An avatar a user set themselves is never overwritten.

> This is separate from the **in-browser Gravatar import** button in the web UI
> (which lets a signed-in user pull their own Gravatar interactively). This script
> is an operator tool that runs across all users at once.

## Running it

Run inside the container (or on the host, in the project's virtual environment):

```bash
# Active users only (default)
python run_sync_gravatar.py

# Also process deactivated/disabled users
python run_sync_gravatar.py --include-disabled

# Pause 200 ms between users to throttle load on Gravatar
python run_sync_gravatar.py --delay-ms 200

# Also fire configured webhooks for each synced avatar
python run_sync_gravatar.py --fire-webhooks
```

For a container deployment, exec into the running container:

```bash
docker exec -it akvatar python run_sync_gravatar.py
```

### Options

| Flag                                     | Default | Description                                                              |
|------------------------------------------|---------|-------------------------------------------------------------------------|
| `--include-deactivated`, `--include-disabled` | off     | Also process deactivated/disabled users (otherwise active users only). |
| `--fire-webhooks`                        | off     | Fire configured webhooks for each synced avatar. Off by default so a bulk run does not send one notification per user. |
| `--delay-ms MS`                          | `0`     | Pause this many milliseconds after each user that made a Gravatar request, to throttle load on Gravatar. Users that are skipped without a request (no email, user-set avatar, removed avatar) do not pause. |

The script has no configuration section of its own. It reuses the existing
`images`, `authentik`, `ldap`, `webhooks`, `dry_run`, and `dry_run_backend`
settings from `config.yml`.

## What it does per user

For each user in scope (active users, plus deactivated users when
`--include-disabled` is passed) that has an email address:

| Situation                                                       | Action                                             |
|-----------------------------------------------------------------|----------------------------------------------------|
| No current avatar, and no avatar was ever recorded for the user | **Import** the Gravatar image.                     |
| Current avatar was applied by this job, Gravatar image changed  | **Update** - re-import the new image.              |
| Current avatar was applied by this job, Gravatar unchanged      | Skip (nothing to do).                              |
| Current avatar was set by the user (upload, URL, webcam, UI Gravatar) | Skip - a user avatar is **never** overwritten. |
| No current avatar, but the user previously had one (removed it) | Skip - not re-added.                               |
| Gravatar returns 404 (no image for that email)                  | Skip; a previously synced avatar is **kept**.      |
| Gravatar serves a format the upload pipeline does not accept (e.g. GIF) | Skip (counted as "unsupported format").     |
| User's avatar changed in Authentik while the run was in progress | Skip - the live record is re-read right before publishing. |
| User has no email address                                       | Skip.                                              |

### How ownership and change detection work

The script relies only on the avatar metadata file (`_metadata/<file>.meta.json`)
that every processed avatar already writes:

- **`source`**: `"web"` for any avatar set through the web UI, or
  `"gravatar_sync"` for avatars this script created. The script only ever touches
  its own (`gravatar_sync`) avatars, so a user-set avatar is safe. A pre-existing
  avatar with no `source` field is treated as a user avatar.
- **`gravatar_hash`**: a SHA-256 of a small, fixed-size (80 px) "probe" image
  fetched from Gravatar, stored on sync avatars only. A later run re-fetches only
  that probe, re-hashes, and downloads the full-size image and re-imports only
  when the hash differs. Hashing a fixed size keeps the stored hash independent
  of `images.sizes`, so changing the configured sizes does not trigger a mass
  re-import. No email address or other PII is stored in the metadata.

Before publishing, the script re-reads the user's live Authentik record and
compares the current `avatar_id` with the one its decision was based on. If they
differ (the user set an avatar through the web UI while a long run was in
progress), the user is skipped and nothing is written.

## Dry-run, safety, and concurrency

- **Dry-run**: honors the global `dry_run` (log-only - nothing is written to disk
  or the backends) and `dry_run_backend` (images are written, but Authentik/LDAP
  writes, webhooks, and the metadata sidecar are skipped) settings. Not writing
  metadata under `dry_run_backend` is deliberate: Authentik was never updated, so
  an ownership record would make the next real run believe the user removed
  their avatar and skip them. The preview files have no metadata and are removed
  by the next cleanup run.
- **Zero-user guard**: if Authentik returns no users (usually a bad token or a
  network problem), the run aborts rather than doing nothing silently.
- **Per-user isolation**: a failure on one user is logged and counted; it never
  aborts the whole run.
- **Concurrency**: a cross-process file lock (`.gravatar_sync.lock` in the avatar
  storage directory) prevents two runs from processing the same users at once. A
  second run started while one is active exits immediately. The run also holds
  the cleanup lock (`.cleanup.lock`) for its duration, so a scheduled cleanup in
  the web app cannot delete freshly generated image files before their metadata
  exists; conversely, the sync exits immediately when a cleanup is in progress.
- **Webhooks**: with `--fire-webhooks`, the script waits (up to 30 s) for any
  still-running webhook deliveries before exiting, so none are lost.

## Output

The script logs a one-line summary at the end, for example:

```text
Gravatar sync complete (128 user(s)): 40 imported, 3 updated, 71 unchanged,
12 skipped (user avatar), 1 skipped (removed), 1 without Gravatar,
0 unsupported format, 0 without email, 0 failed.
```

### Exit codes

| Code | Meaning                                                                                   |
|------|-------------------------------------------------------------------------------------------|
| `0`  | The run completed and every user was processed without failure.                           |
| `1`  | The run completed but at least one user failed (fetch error, backend error, ...).         |
| `2`  | Nothing ran: another sync or a cleanup holds the lock, the user list could not be fetched, or Authentik returned zero users. |

## Notes

- Users with no avatar and no Gravatar are re-checked on every run (one `d=404`
  request each), since there is no record to say "no Gravatar yet". Users with a
  sync-owned avatar cost one small 80 px probe request per run. Use
  `--delay-ms` to throttle if this matters at your scale.
- The Gravatar image is requested at the largest configured avatar size (capped at
  Gravatar's 2048 px maximum) so the source is at least as large as every generated
  output.
