# Production deployment and rollback

This runbook deploys a reviewed, merged `main` commit behind the existing Huangque TLS virtual host. It does not authorize a deployment by itself. Complete the release review, nested CI, dependency audit, and change approval first.

## Safety prerequisites

- Use a named, non-root operator where possible and SSH public-key authentication. Disable password automation; never paste passwords into commands, logs, tickets, or environment files.
- Confirm the checked-out commit is the approved merged `main` SHA.
- Install Node.js 22.13 or newer, Docker with Compose v2, Nginx, Chrome/Chromium, FFmpeg, and `ffprobe`.
- Keep PostgreSQL, Redis, and MinIO Docker-internal. Only Nginx is public; the API publishes `127.0.0.1:4173`.
- Store production secrets in a server-only environment file outside the repository, owned by the deployment account and mode `0600`. Generate high-entropy values on the server. `DATABASE_URL` must contain the same PostgreSQL identity as Compose and percent-encode reserved password characters. `HUANGQUE_AUTH_BASE` must be the reachable HTTPS Huangque authentication origin. Configure credential-free public HTTPS origins for `AVATAR_PROVIDER_ENDPOINT`, `IMAGE_PROVIDER_ENDPOINT`, and `RENDER_PROVIDER_ENDPOINT`; list every approved provider CDN origin (and no private origin) in comma-separated `PROVIDER_ALLOWED_MEDIA_ORIGINS`. Keep `PROVIDER_TOKEN` only in that protected environment file and never place it in a URL, log, repository file, or provider response.
- Obtain a maintenance window, a backup destination with sufficient space, and an operator who can exercise the rollback.

Never run `docker compose down -v`, `docker volume rm`, or any cleanup command that deletes persistent volumes during deployment or rollback.

## 1. Read-only preflight and rollback record

From the repository root, capture output in the protected change record (redact environment values):

```bash
git rev-parse HEAD
git status --short
node --version
docker version
docker compose version
nginx -v
ffmpeg -version
ffprobe -version
test "$(stat -c '%a' /protected/path/video-workbench.env)" = "600"
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml ps
docker volume ls
df -h
```

Record:

- current application commit and intended commit;
- active Nginx configuration path and a checksum/copy of it;
- Compose project name, service state, image digests, and persistent volume names;
- current `/healthz` result through loopback and through the TLS site;
- database migration/backup timestamps.

Render the Compose model only after loading the protected environment file. Do not paste its output because interpolated secrets may appear:

```bash
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml config --quiet
```

Stop if the worktree is dirty, secrets are missing, the auth HTTPS origin is unreachable, disk space is insufficient, backups fail, or the API port is not loopback-only.

## 2. Back up before changing state

Compose pins application state to the stable named volumes
`huangque-video-workbench-postgres-data` and
`huangque-video-workbench-minio-data`. Confirm both names exist before changing
state. Create a protected backup directory outside these volumes, then make and
validate the PostgreSQL logical backup:

```bash
install -d -m 0700 /protected/backups/video-workbench
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml exec -T postgres \
  pg_dump -U huangque -d huangque -Fc > /protected/backups/video-workbench/postgres.dump
test -s /protected/backups/video-workbench/postgres.dump
pg_restore --list /protected/backups/video-workbench/postgres.dump >/dev/null
```

Create and validate a MinIO data backup from the read-only stable volume. Stop
application writers for this filesystem-level snapshot, or use the site's
storage-native consistent snapshot mechanism instead:

```bash
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml stop api worker
docker run --rm \
  -v huangque-video-workbench-minio-data:/source:ro \
  -v /protected/backups/video-workbench:/backup \
  alpine:3.22 tar -C /source -czf /backup/minio-data.tar.gz .
test -s /protected/backups/video-workbench/minio-data.tar.gz
tar -tzf /protected/backups/video-workbench/minio-data.tar.gz >/dev/null
```

Record checksums and timestamps for both artifacts. This MinIO data backup is
the restore point for objects; the PostgreSQL dump is the database restore
point. Back up the active Nginx file and server-only environment file without
printing their contents.

MinIO objects and Redis/PostgreSQL volumes are not deleted or recreated by this procedure. Apply the site's established snapshot policy before proceeding.

## 3. Build, verify, and migrate

At the approved commit:

```bash
npm ci
npm run typecheck
npm run build
npm test
npm run db:verify
npm audit --omit=dev --audit-level=high
```

Start dependencies first and wait for healthy status:

```bash
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml up -d postgres redis minio
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml ps
```

Run the additive migrations as the one-shot `migrate` service. A non-zero exit stops the deployment:

```bash
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml run --rm migrate
```

Do not attempt destructive schema reversal during application rollback. This release's ownership, revision, and outbox additions are additive and remain in place.

## 4. Staged application startup

Start the API, verify its loopback health, then start the worker:

```bash
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml up -d api
curl --fail --silent --show-error http://127.0.0.1:4173/healthz
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml up -d worker
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml ps
```

Inspect API, worker, migration, Redis, PostgreSQL, and MinIO logs for startup errors, repeated dispatch failures, lease loss, authentication outages, or unexpected restarts. Never use commands that print the environment.

Run executable service checks without printing secret values:

```bash
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml exec -T postgres pg_isready -U huangque -d huangque
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml exec -T redis sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping'
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml exec -T minio mc ready local
docker compose --env-file /protected/path/video-workbench.env -f infra/docker-compose.yml ps --status running api worker
curl --fail --silent --show-error http://127.0.0.1:4173/healthz
```

## 5. Install and validate Nginx

Include `infra/nginx-video-workbench.conf` inside the existing Huangque HTTPS `server` block. It supplies two prefix-stripping mounts:

- `/video-workbench/` for the workbench pages;
- `/api/video-workbench/` for API, authenticated output, and SSE requests.

The production application emits the explicit `/api/video-workbench/...` base path for API, SSE, and authenticated output links. Validate that the existing Huangque site forwards that exact prefix before enabling traffic.

Test before reload:

```bash
nginx -t
```

Only after a successful syntax test, use the operating system's normal graceful Nginx reload and confirm `nginx -t` again.

## 6. Post-deploy acceptance

Use HTTPS and a dedicated test account. Do not expose its cookie value in shell history or logs.

1. Confirm `GET /api/video-workbench/healthz` succeeds and reveals no project data.
2. Confirm an unauthenticated project request returns `401`, an authentication-service outage fails closed with `503`, and a valid browser session succeeds.
3. Create projects as two test users and confirm neither can read, subscribe to SSE, edit, render, or download the other's project (`404`).
4. Run the acceptance script against the deployed HTTPS origin. It must use
   production-capable avatar, voice, and template identifiers, reach
   `COMPLETED`, download through the authenticated endpoint, and pass
   `ffprobe`. Create both protected files without putting credentials or
   identifiers in command history. Use a trusted editor to populate the payload
   file with exactly `{"avatarId":"...","voiceId":"...","templateId":"..."}`:

```bash
install -m 0600 /dev/null /protected/path/deployment-test.cookie
install -m 0600 /dev/null /protected/path/deployment-payload.json
# Populate both files using a trusted interactive editor; do not echo their contents.
chmod 0600 /protected/path/deployment-test.cookie
chmod 0600 /protected/path/deployment-payload.json
DEPLOYED_WORKBENCH_BASE_URL=https://huangque.example \
DEPLOYED_WORKBENCH_COOKIE_FILE=/protected/path/deployment-test.cookie \
DEPLOYED_WORKBENCH_PAYLOAD_FILE=/protected/path/deployment-payload.json \
npm run acceptance:deployed
```

The cookie file contains a single `hq_session=...` line. Alternatively, use `DEPLOYED_WORKBENCH_HEADER_FILE` containing a single `Cookie: hq_session=...` line, also mode `0600`. The payload schema rejects missing, empty, unknown, or overlong identifiers before any request and never prints them. Never configure both credential forms, never print either protected file, and remove both temporary files immediately after acceptance. This command connects to the deployed URL; it does not start the local fixture composition, use local mock identifiers, or bypass Huangque authentication.
5. Verify the private MinIO object is not anonymously readable and PostgreSQL, Redis, and MinIO have no public host bindings.
6. Probe the resulting file:

```bash
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration -of json /protected/path/to/accepted-output.mp4
```

Confirm H.264 video, AAC audio, expected dimensions/duration, audible content, and non-static avatar/B-roll intervals. Exercise an SSE request long enough to confirm it is streamed rather than buffered.

## 7. Monitoring

During the change window watch:

- `/healthz`, Nginx 4xx/5xx and upstream latency;
- API/worker restarts, event-loop or memory pressure;
- BullMQ queue depth, pending outbox age, dispatch attempts, lease loss, and terminal failures;
- PostgreSQL connections/storage, Redis health, MinIO errors/storage;
- auth upstream `401`/`503` rates and authenticated output failures.

Define alert owners and thresholds in the existing monitoring system. Keep logs free of cookies, database URLs, object credentials, and signed URLs.

## Rollback

Rollback on failed health/auth/isolation checks, persistent queue failures, rendering regression, Nginx errors, or unacceptable error rates:

1. Stop new traffic using the approved maintenance control; preserve logs and timestamps.
2. Restore the previous application commit or immutable images recorded in preflight.
3. Restore the saved Nginx configuration, run `nginx -t`, and gracefully reload it.
4. Rebuild/start `api` and `worker` at the previous commit while retaining the same stable named volumes: `huangque-video-workbench-postgres-data` and `huangque-video-workbench-minio-data` (and the existing Redis state). Confirm Compose resolves those exact names before startup.
5. Leave additive migrations in place. Do not run down-with-volumes or manually drop new columns/tables.
6. Repeat loopback health, unauthenticated rejection, authenticated isolation, queue, output, and Nginx checks.
7. If data restoration is explicitly approved, stop all writers first and follow the database team's tested restore procedure using the verified backup. This is a separate destructive change, not the default application rollback.

Record the rollback commit, service state, health evidence, incident owner, and follow-up actions.
