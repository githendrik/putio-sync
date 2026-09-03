# AGENTS.md

## Project overview

putio-sync is a thin Flask web UI that drives rclone's rc API to selectively
sync local folders to put.io. The app never touches file data — it only calls
rclone rc endpoints to list, start, and monitor sync jobs.

## Architecture

```
Browser -> Flask (app.py, port 8080) -> rclone rcd (port 5572) -> put.io
```

- `app.py` — Flask backend. Calls rclone rc API via `urllib`. SQLite for job persistence.
- `templates/index.html` — Single-page frontend. Vanilla JS, no build step, no frameworks.
- No external JS/CSS dependencies. Everything is inline.

## Key design decisions

- **No rclone WebUI dependency.** The built-in rclone-webui-react doesn't support
  folder-to-remote sync jobs from the UI. This app replaces it.
- **`sync/copy` not `sync/sync`.** Copies new/changed files, does not delete from
  put.io. Prevents accidental data loss.
- **Async jobs.** All sync operations use `_async=true` and are tracked by rclone
  job ID. The frontend polls `/api/status` every 5 seconds.
- **Job recovery.** If rclone rcd is restarted, job IDs become invalid. The app
  catches the HTTP 500 from `job/status` and marks the job as completed.

## Configuration

All config is via environment variables (see README.md). The key one:

- `PUTIO_DEST_ROOT` — the put.io folder used as the destination root in the UI.
  Injected into the template via Jinja2 (`{{ dest_root }}`).

## rclone rc API gotchas

- `operations/list` requires `fs` (remote name with colon, e.g. `data1:`) and
  `remote` (path within that remote, e.g. `Movies`) as **separate** parameters.
  Do not combine them into `fs=data1:Movies`.
- `core/stats` with `group=job/<id>` returns per-job transfer stats including
  `percentage` (0-100, do not multiply), `bytes`, `totalBytes`, `speed`, `eta`.
- `job/status` returns 500 (not 404) when a job ID doesn't exist after rclone
  restart. Must catch `urllib.error.HTTPError` with code 500.
- rc API calls must be POST with `Content-Type: application/json` and a JSON body.
  Form-encoded data does not work for nested params like `opt`.

## Deployment

App lives at `/opt/putio-sync/` in the LXC. Two systemd services:
- `rclone-rcd.service` — rclone rc daemon on port 5572
- `putio-sync.service` — Flask app on port 8080 (depends on rclone-rcd)

Deploy updates:
```bash
scp app.py lab:/tmp/putio-sync/app.py
scp templates/index.html lab:/tmp/putio-sync/templates/index.html
ssh lab "pct push 117 /tmp/putio-sync/app.py /opt/putio-sync/app.py && \
         pct push 117 /tmp/putio-sync/templates/index.html /opt/putio-sync/templates/index.html && \
         pct exec 117 -- systemctl restart putio-sync.service"
```

## Conventions

- No comments in code unless requested
- No external dependencies beyond Flask (use stdlib for everything else)
- Frontend is vanilla JS — no npm, no bundler, no frameworks
- Keep the app single-file (`app.py`) — it's small enough
