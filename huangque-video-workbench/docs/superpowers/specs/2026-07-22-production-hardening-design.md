# Huangque Video Workbench Production Hardening Design

## Goal

Make the standalone video workbench safe and correct to merge into the existing Huangque repository and deploy behind its existing authenticated web application.

## Release gates

The PR must not merge until all Critical and Important review findings are fixed, the nested-project CI job passes, the production dependency audit has no high or critical findings, and an independent re-review approves the result. Deployment must use SSH public-key authentication, a rollback point, database migrations, and post-deploy health checks.

## Authentication and ownership

The workbench reuses Huangque's existing `HttpOnly` `hq_session` cookie. For every protected request, the Node API forwards only the session cookie to the existing authentication service at `http://127.0.0.1:8095/api/auth/me`. A successful response supplies the canonical username; unavailable authentication fails closed with `503`, and invalid or absent authentication returns `401`.

Projects gain a non-null `ownerUsername`. Create operations set it from the verified session. Project reads, scene edits, regeneration, render requests, SSE subscriptions, and output delivery require the same owner. No client-provided username is trusted. Health checks remain unauthenticated and reveal no project data.

Browser requests use the existing same-origin cookie. Nginx exposes the UI below `/video-workbench/` and the API below `/api/video-workbench/`; the workbench itself does not introduce credentials, login forms, or browser-readable access tokens.

## Network and secrets

PostgreSQL, Redis, and MinIO are Docker-internal only and have no public host bindings. The API is bound to `127.0.0.1` on the host, with Nginx as the only public entry point. Redis requires authentication in production. PostgreSQL, Redis, and MinIO credentials come from a server-side environment file with restrictive permissions and are never committed.

Compose must reject missing production secrets instead of falling back to `localdev`. Development defaults belong in a separate explicitly named development override. The API trusts forwarded headers only through the local Nginx boundary.

## Private video delivery

MinIO buckets and objects remain private; startup must never install an anonymous-read policy. Worker results store stable object keys, not public URLs. The API offers an authenticated output endpoint that checks project ownership and then either streams the object or returns a short-lived presigned URL. The initial implementation uses authenticated streaming to avoid placing bearer material in URLs or logs.

## Production task correctness

BullMQ handles all task names emitted by the API, including `project.render`. A render request invokes the existing final render pipeline idempotently and persists terminal status and provenance. Unknown task names remain unrecoverable errors.

Scene PATCH accepts a typed allowlist of editable business fields. It updates the canonical scene representation consumed by the worker and preserves orchestration metadata such as active generation identifiers. Regeneration and final rendering must demonstrably consume the edited values.

## Durable queue recovery

Database job rows remain the durable source of truth. A bounded outbox dispatcher scans due `PENDING` jobs, submits them to BullMQ with the database job ID as the queue job ID, and marks dispatch success only after enqueue succeeds. Startup and an interval timer run the dispatcher; duplicate dispatch is safe. Failed dispatch records retry metadata and exponential backoff. Shutdown stops the timer and waits within a fixed deadline.

## Dependency and CI policy

Upgrade `drizzle-orm` to a version that resolves GHSA-gpj5-g38j-94v9, with `0.45.2` as the minimum acceptable version. Re-run repository and migration tests after the upgrade.

The parent repository CI adds a path-aware job whose working directory is `huangque-video-workbench`. It runs Node 22, `npm ci`, typecheck, build, unit tests, database migration verification, Docker Compose configuration validation, and `npm audit --omit=dev --audit-level=high`. The job triggers when the nested project or its workflow changes.

## Deployment and rollback

Deployment uses the merged `main` commit. Before changes, record the active commit, Compose configuration, persistent volume names, and service health. Generate server-only secrets, run migrations as a one-shot service, start dependencies, then API and worker, and finally install the Nginx locations. Validate authentication rejection, authenticated project isolation, health endpoints, queue processing, private output delivery, and a fixture render.

Rollback restores the previous application commit and Nginx configuration without deleting persistent volumes. Database migrations in this release are additive; rollback leaves the added ownership and queue metadata columns in place.

## Tests

Automated tests must cover missing/invalid/valid sessions, auth-service outages, cross-user access denial, authenticated SSE and output delivery, absence of anonymous MinIO policy, loopback/internal-only Compose exposure, render-job execution, edited-scene regeneration, outbox recovery and deduplication, shutdown deadlines, secure dependency versions, and parent CI invocation of all nested gates.

## Out of scope

This hardening does not replace Huangque authentication, migrate the entire site to a new identity protocol, add billing, or enable public object sharing. Real paid generation providers remain a separate configuration and acceptance concern.
