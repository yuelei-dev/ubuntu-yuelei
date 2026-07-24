# Local development

## Prerequisites

- Node.js 22.13 or newer and npm.
- Google Chrome. The scripts discover standard Windows installations; otherwise set `HUANGQUE_CHROME_PATH` to the local Chrome executable.
- `ffmpeg` and `ffprobe` on `PATH`.
- Docker Desktop only for the optional PostgreSQL/Redis/MinIO checks.

Copy `env.sample` to `.env` only if you need to override a local default. The sample intentionally contains no credentials.

Install the workspace from the repository root:

```bash
npm install
```

On Windows PowerShell installations that block `npm.ps1`, use `npm.cmd` in place of `npm` for every command below.

## Default in-process fixture mode

The default mode needs no Docker or remote credentials. It starts Fastify on loopback, uses a shared in-memory project repository and queue, runs the real worker processor with deterministic providers, bundles the fixture Remotion composition, finalizes with FFmpeg, and runs the media quality gate.

The local acceptance composition intentionally draws each frame on one canvas using the generated avatar/image pixels, title, and timed captions. This works around a Chrome capture incompatibility that rendered nested production React scene layers as a flat surface on the supported Windows test host. The production renderer under `packages/renderer` remains unchanged. To retire the workaround after upgrading Chrome/Remotion, point the bundle entry in `start-local-composition.ts` back to `packages/renderer/src/index.ts`, select `VerticalKnowledgeVideo`, and keep the ffmpeg visual-analysis gate green.

Run a complete fixture render:

```bash
npm run fixture
```

The runner:

1. starts the composition on a free loopback port;
2. creates the planned Chinese fixture through `POST /api/projects`;
3. polls the HTTP project resource until a terminal state, with a 120-second deadline;
4. requires `COMPLETED` and a `/preview.mp4` URL;
5. writes a stable copy to `tests/output/final.mp4`; and
6. closes the API, queue, renderer browser, and worker path through typed, bounded shutdown deadlines.

Each render uses a UUID-named directory under the operating-system temporary directory, so parallel or repeated runs do not share intermediate video or quality-report paths. A failed terminal state, missing Chrome/FFmpeg, malformed API response, or timeout exits nonzero with an actionable message.

## Interactive browser flow

Start the local composition:

```bash
npm run dev
```

Open `http://127.0.0.1:4173/projects/new`, enter a script, avatar, voice, and template, then select **Create project**. The project page reads API state and SSE progress from the same process and exposes the MP4 only after the quality gate passes. Press Ctrl+C once to close the listener and in-process queue.

To use another port:

```powershell
$env:HUANGQUE_PORT='4180'
npm.cmd run dev
```

## Tests and build

Run the complete deterministic verification set:

```bash
npm run typecheck
npm run build
npm test
npm run db:verify
npm run test:e2e
npm run fixture
```

`test:e2e` launches the local composition, uses the installed Chrome explicitly, creates the project through the browser, observes an intermediate progress state and then `COMPLETED`, verifies per-scene generated-asset coverage, and checks the preview MP4. It also samples avatar and B-roll intervals through ffmpeg and rejects uniform output or missing title/caption-region detail.

Verify the final media independently:

```bash
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration -of json tests/output/final.mp4
```

Expected values are:

- video codec `h264`;
- audio codec `aac`;
- width `1080` and height `1920`; and
- duration `9.367` seconds, within `0.100` seconds of the fixture timeline.

## Database migrations

The baseline migration is in `apps/api/drizzle`. It creates `projects`, `scenes`, `jobs`, and `asset_versions`, including project quality-report/output/failure fields and persisted job options.

Migration verification does not require Docker. It checks Drizzle metadata, applies every SQL statement to an in-process PostgreSQL-compatible PGlite database, and asserts the resulting tables and unique constraint:

```bash
npm run db:verify
```

After changing the Drizzle schema, generate and check a migration:

```bash
npm run db:generate
npm run db:verify
```

## Docker production-like mode

The Compose stack runs PostgreSQL, Redis/BullMQ, MinIO, a one-shot migration, the Fastify API, and a separate worker. The worker persists durable state in PostgreSQL, consumes BullMQ jobs from Redis, and uploads the preview and quality report to MinIO.

Build and start the production-like stack with a persistent server-side environment file (for example `/etc/huangque/video-workbench.env`, mode `0600`, never in Git). It must contain `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, a percent-encoded `DATABASE_URL`, and an HTTPS `HUANGQUE_AUTH_BASE`.

```bash
docker compose --env-file /etc/huangque/video-workbench.env -f infra/docker-compose.yml up -d
```

Open `http://127.0.0.1:4173/projects/new`. PostgreSQL, Redis, and MinIO have no host bindings; MinIO objects remain private and the API streams output only after the Huangque owner check. The API is loopback-only so Nginx can be the public entry point. For explicitly local dependency debugging, use the development override together with its explicit development environment file; it exposes only loopback dependency ports:

```bash
docker compose --env-file infra/docker-compose.dev.env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up
```

The development override starts an internal-only `auth-dev` service and, only when it serves the workbench browser entry, bootstraps the `HttpOnly; SameSite=Lax` `hq_session=localdev` cookie. A fresh browser can therefore use the local workbench immediately. This identity and HTTP auth origin exist only in the development override; production uses the supplied HTTPS Huangque identity service and never sets a development session.

Stop the production stack with the same persistent environment file:

```bash
docker compose --env-file /etc/huangque/video-workbench.env -f infra/docker-compose.yml down
```

Stop the development override with:

```bash
npm run infra:down:dev
```

The one-command `npm run fixture` acceptance path deliberately remains in-process so it is deterministic and independent of Docker availability. Static tests validate the Compose dependencies and every required runtime environment variable. A live Compose run additionally requires a working Docker daemon.

## Troubleshooting

- **Chrome was not found:** set `HUANGQUE_CHROME_PATH` to an installed `chrome.exe`. Do not point it at a Chrome profile directory.
- **`ffmpeg` or `ffprobe` is not recognized:** install FFmpeg and add its `bin` directory to `PATH`, then open a new shell.
- **Port 4173 is already in use:** stop the listener or set `HUANGQUE_PORT` to another loopback port. Playwright intentionally refuses to reuse an existing server.
- **The project reaches `FAILED`:** inspect the typed error printed by the fixture. Successful local output is copied to `tests/output/final.mp4`; temporary project directories are removed during shutdown.
- **The wait reaches 120 seconds:** confirm Chrome and FFmpeg can launch locally and that endpoint security is not blocking child processes. The runner always exits rather than polling forever.
- **Docker is unavailable:** use `npm run fixture` and `npm run db:verify`; only the live Compose path requires Docker. The Docker YAML and environment contract are still covered by `npm test`.
- **PowerShell refuses to run npm:** invoke `npm.cmd`, for example `npm.cmd run test:e2e`.
