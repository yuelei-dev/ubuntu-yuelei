# Huangque Video Workbench

Huangque turns a script into a deterministic vertical knowledge video. The default local fixture composes the real API, director, worker pipeline, deterministic providers, Remotion capture, FFmpeg finalizer, and quality gate in one process; project completion is never simulated in the page.

## Quick start

Install Node.js 22.13 or newer, Google Chrome, and FFmpeg (including `ffprobe`), then run:

```bash
npm install
npm run fixture
```

The command creates the planned mixed avatar/visual project through HTTP, waits at most 120 seconds for `COMPLETED`, writes `tests/output/final.mp4`, prints its project/preview metadata, and shuts the local stack down.

For the browser workbench:

```bash
npm run dev
```

Open `http://127.0.0.1:4173/projects/new`. Press Ctrl+C to stop it cleanly.

## Verification

```bash
npm run typecheck
npm run build
npm test
npm run db:verify
npm run test:e2e
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration -of json tests/output/final.mp4
```

The probe must report H.264 video, AAC audio, 1080×1920, and `9.367` seconds (within 0.100 seconds).

See [Local development](docs/operations/local-development.md) for Docker-backed migration checks, the exact workflow, and troubleshooting.

## Production operations

Production must stay behind the existing Huangque HTTPS/authentication boundary. The supplied Nginx include proxies only the two workbench mount paths to the loopback API; PostgreSQL, Redis, and MinIO remain private.

Before any release, follow the complete [production deployment and rollback runbook](docs/operations/production-deployment.md). It requires SSH public keys, server-generated secrets, backups, staged startup, authenticated owner-isolation checks, fixture/`ffprobe` acceptance, monitoring, and a rollback that preserves persistent volumes.

`npm run fixture` is the self-contained local deterministic check. Production acceptance is deliberately separate: `npm run acceptance:deployed` requires an HTTPS deployed origin and a mode-`0600` Huangque session cookie/header file, and never starts a local composition or bypasses authentication.
