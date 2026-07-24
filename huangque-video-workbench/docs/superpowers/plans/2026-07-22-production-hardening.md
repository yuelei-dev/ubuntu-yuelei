# Video Workbench Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every release-blocking security and production-correctness finding from PR #111 and make the nested workbench safe to merge and deploy behind Huangque authentication.

**Architecture:** The Node API verifies Huangque's `hq_session` with the existing auth service and enforces database-backed project ownership. Infrastructure stays private behind Nginx, MinIO delivery is authenticated, BullMQ gains render and durable outbox processing, and parent CI invokes the nested project's full release gates.

**Tech Stack:** TypeScript 5.9, Fastify, Drizzle/PostgreSQL, BullMQ/Redis, MinIO, Remotion, Vitest, Docker Compose, GitHub Actions, Nginx.

## Global Constraints

- Node.js must be `>=22.13.0`.
- No public PostgreSQL, Redis, MinIO, or unauthenticated API listener.
- Reuse `HttpOnly` `hq_session`; do not introduce browser-readable tokens.
- Every project resource is owner-scoped and fails closed when auth is unavailable.
- MinIO objects remain private; no anonymous bucket policy.
- `drizzle-orm` must be at least `0.45.2` and production audit must have zero high/critical findings.
- Every production behavior change follows red-green-refactor and receives an independent review before merge.

---

### Task 1: Huangque Authentication and Project Ownership

**Files:**
- Create: `apps/api/src/auth/huangque-auth.ts`
- Create: `apps/api/src/auth/huangque-auth.test.ts`
- Modify: `apps/api/src/app.ts`
- Modify: `apps/api/src/routes/projects.ts`
- Modify: `apps/api/src/db/schema.ts`
- Modify: `apps/api/src/db/drizzle-project-repository.ts`
- Create: `apps/api/drizzle/0001_project_ownership.sql`
- Test: `apps/api/src/routes/projects.test.ts`
- Test: `apps/api/src/services/project-repository.test.ts`

**Interfaces:**
- Produces: `HuangqueIdentity {username: string; role: string}` and `authenticateHuangque(cookieHeader, signal): Promise<HuangqueIdentity>`.
- Produces: repository methods whose project reads and writes require `ownerUsername`.

- [ ] **Step 1: Write failing auth and cross-owner tests**

```ts
it('rejects a project owned by another Huangque user', async () => {
  const response = await app.inject({method: 'GET', url: `/projects/${project.id}`, headers: {cookie: 'hq_session=bob'}});
  expect(response.statusCode).toBe(404);
});
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `npm test -- apps/api/src/auth/huangque-auth.test.ts apps/api/src/routes/projects.test.ts`
Expected: failures because auth integration and ownership do not exist.

- [ ] **Step 3: Implement fail-closed auth and ownership migration**

```ts
export type HuangqueIdentity = {username: string; role: string};
export type HuangqueAuthenticator = (cookieHeader: string, signal?: AbortSignal) => Promise<HuangqueIdentity>;
```

Forward only `Cookie` to `/api/auth/me`, map `401` to an authentication error and upstream failures to `503`. Add non-null `owner_username`, set it from the authenticated identity, and scope every repository query by owner.

- [ ] **Step 4: Verify GREEN and migrations**

Run: `npm test -- apps/api/src/auth/huangque-auth.test.ts apps/api/src/routes/projects.test.ts apps/api/src/services/project-repository.test.ts && npm run db:verify`
Expected: all focused tests and migration checks pass.

- [ ] **Step 5: Commit**

```bash
git add apps/api
git commit -m "feat: enforce Huangque project ownership"
```

### Task 2: Private Infrastructure and Authenticated Output Delivery

**Files:**
- Modify: `infra/docker-compose.yml`
- Create: `infra/docker-compose.dev.yml`
- Modify: `infra/docker-compose.test.ts`
- Modify: `scripts/docker-api.ts`
- Modify: `scripts/docker-worker.ts`
- Create: `apps/api/src/routes/output.ts`
- Create: `apps/api/src/routes/output.test.ts`
- Modify: `apps/api/src/app.ts`
- Modify: `docs/operations/local-development.md`

**Interfaces:**
- Consumes: authenticated identity and owner-scoped repository from Task 1.
- Produces: `ObjectReader.open(objectKey): Promise<Readable>` and `GET /projects/:projectId/output`.

- [ ] **Step 1: Write failing security and output tests**

```ts
expect(compose.services.redis.ports).toBeUndefined();
expect(workerSource).not.toContain('s3:GetObject');
expect(crossOwnerOutput.statusCode).toBe(404);
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- infra/docker-compose.test.ts apps/api/src/routes/output.test.ts`
Expected: current public ports, anonymous policy, and missing route fail.

- [ ] **Step 3: Implement private networking and streaming**

Remove production host ports for PostgreSQL, Redis, and MinIO; bind API as `127.0.0.1:4173:4173`; require `${POSTGRES_PASSWORD:?}`, `${REDIS_PASSWORD:?}`, `${MINIO_ROOT_USER:?}`, and `${MINIO_ROOT_PASSWORD:?}`. Remove anonymous bucket policy and stream owner-authorized objects from MinIO through Fastify.

- [ ] **Step 4: Verify GREEN**

Run: `npm test -- infra/docker-compose.test.ts apps/api/src/routes/output.test.ts && docker compose -f infra/docker-compose.yml config`
Expected: tests pass; Compose either validates with test secrets or reports only explicitly missing required secrets.

- [ ] **Step 5: Commit**

```bash
git add infra scripts apps/api/src/routes apps/api/src/app.ts docs/operations
git commit -m "fix: keep workbench data private"
```

### Task 3: Production Render Job

**Files:**
- Modify: `apps/worker/src/bullmq-worker.ts`
- Modify: `apps/worker/src/bullmq-worker.test.ts`
- Modify: `apps/worker/src/pipeline.ts`
- Modify: `scripts/start-local-composition.ts`

**Interfaces:**
- Produces: `processRenderJob(projectId, signal): Promise<void>` invoked for BullMQ name `project.render`.

- [ ] **Step 1: Write a failing render dispatch test**

```ts
await processor({name: 'project.render', data: {projectId}} as Job);
expect(repository.getProject(projectId)).resolves.toMatchObject({status: 'COMPLETED'});
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- apps/worker/src/bullmq-worker.test.ts`
Expected: `project.render` is rejected as an unknown job.

- [ ] **Step 3: Route render jobs into the idempotent finalizer**

Use the existing render/finalization pipeline, preserve terminal results on duplicate delivery, and remove the local fixture no-op.

- [ ] **Step 4: Verify GREEN**

Run: `npm test -- apps/worker/src/bullmq-worker.test.ts apps/worker/src/pipeline-worker.test.ts`
Expected: render dispatch, duplicate delivery, and failure-state tests pass.

- [ ] **Step 5: Commit**

```bash
git add apps/worker scripts/start-local-composition.ts
git commit -m "fix: execute production render jobs"
```

### Task 4: Canonical Scene Editing

**Files:**
- Modify: `packages/contracts/src/storyboard.ts`
- Modify: `apps/api/src/routes/projects.ts`
- Modify: `apps/api/src/db/drizzle-project-repository.ts`
- Modify: `apps/worker/src/pipeline.ts`
- Test: `apps/api/src/routes/projects.test.ts`
- Test: `apps/worker/src/pipeline-worker.test.ts`

**Interfaces:**
- Produces: `EditableScenePatchSchema` allowing only `script`, `visualPrompt`, and approved presentation fields.
- Produces: `updateScene(ownerUsername, sceneId, patch)` that atomically preserves orchestration metadata.

- [ ] **Step 1: Write failing edit-regenerate tests**

```ts
await patchScene({script: 'edited narration'});
await regenerateScene(sceneId);
expect(provider.lastRequest.script).toBe('edited narration');
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- apps/api/src/routes/projects.test.ts apps/worker/src/pipeline-worker.test.ts`
Expected: regenerated request still uses the old worker scene or metadata disappears.

- [ ] **Step 3: Implement canonical typed updates**

Parse with `EditableScenePatchSchema`, merge editable fields into the canonical worker scene, preserve active job identifiers and provenance, and reject unknown keys with `400`.

- [ ] **Step 4: Verify GREEN**

Run: `npm test -- apps/api/src/routes/projects.test.ts apps/worker/src/pipeline-worker.test.ts`
Expected: edited values drive regeneration and protected metadata remains intact.

- [ ] **Step 5: Commit**

```bash
git add packages/contracts apps/api apps/worker
git commit -m "fix: make scene edits canonical"
```

### Task 5: Durable Outbox Dispatcher

**Files:**
- Create: `apps/api/src/outbox/dispatcher.ts`
- Create: `apps/api/src/outbox/dispatcher.test.ts`
- Modify: `apps/api/src/db/schema.ts`
- Modify: `apps/api/src/db/drizzle-project-repository.ts`
- Create: `apps/api/drizzle/0002_outbox_retry.sql`
- Modify: `apps/api/src/production.ts`
- Modify: `apps/api/src/services/project-service.ts`

**Interfaces:**
- Produces: `OutboxDispatcher.start(): void`, `dispatchOnce(signal): Promise<number>`, and `close(deadlineMs): Promise<void>`.
- Consumes: pending job rows and BullMQ queue adapter; queue job ID equals database job ID.

- [ ] **Step 1: Write failing outage and deduplication tests**

```ts
await expect(service.createProject(input)).resolves.toBeDefined();
queue.recover();
await dispatcher.dispatchOnce();
expect(queue.jobsFor(dbJob.id)).toHaveLength(1);
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- apps/api/src/outbox/dispatcher.test.ts`
Expected: no dispatcher exists and pending jobs remain undispatched.

- [ ] **Step 3: Implement bounded recovery**

Add `dispatch_attempts` and `next_dispatch_at`; claim due rows with database locking, enqueue idempotently, record success, and retry failures with capped exponential backoff. Start on boot and poll every five seconds; close within ten seconds.

- [ ] **Step 4: Verify GREEN and migration checks**

Run: `npm test -- apps/api/src/outbox/dispatcher.test.ts apps/api/src/services/project-repository.test.ts && npm run db:verify`
Expected: outage recovery, duplicates, concurrency, backoff, and shutdown pass.

- [ ] **Step 5: Commit**

```bash
git add apps/api
git commit -m "feat: recover pending queue jobs"
```

### Task 6: Secure Dependencies and Parent CI

**Files:**
- Modify: `apps/api/package.json`
- Modify: `package-lock.json`
- Create locally: `integration/video-workbench.yml`
- Publish in parent PR as: `.github/workflows/video-workbench.yml`
- Create: `tests/parent-ci-contract.test.ts`
- Modify: `package.json`

**Interfaces:**
- Produces: a workflow using `defaults.run.working-directory: huangque-video-workbench` and Node 22.
- The workflow must configure a PostgreSQL service, set
  `TEST_POSTGRES_DATABASE_URL`, and run `npm run test:postgres-integration`.
  This command intentionally exits non-zero when the variable is absent.
- The Task 6 parent-CI contract test must assert the PostgreSQL service,
  `TEST_POSTGRES_DATABASE_URL`, and mandatory integration command are present;
  ordinary `npm test` is not a substitute because it may honestly skip the
  environment-dependent isolation test.

- [ ] **Step 1: Write a failing workflow contract test**

```ts
expect(workflow).toContain('working-directory: huangque-video-workbench');
expect(workflow).toContain('npm audit --omit=dev --audit-level=high');
expect(workflow).toContain('npm run test:postgres-integration');
expect(workflow).toContain('TEST_POSTGRES_DATABASE_URL');
```

- [ ] **Step 2: Verify RED and audit evidence**

Run: `npm test -- tests/parent-ci-contract.test.ts; npm audit --omit=dev --audit-level=high`
Expected: workflow test fails and audit reports the current Drizzle advisory.

- [ ] **Step 3: Upgrade Drizzle and add nested CI**

Pin `drizzle-orm` to `^0.45.2`, refresh the lockfile, and add a path-filtered workflow that runs install, typecheck, build, tests, migration verification, Compose config validation with CI-only secrets, and production audit.

- [ ] **Step 4: Verify GREEN**

Run: `npm ci && npm run typecheck && npm run build && npm test && npm run db:verify && npm audit --omit=dev --audit-level=high`
Expected: every command exits zero and audit has no high/critical findings.

- [ ] **Step 5: Commit**

```bash
git add apps/api/package.json package-lock.json package.json integration/video-workbench.yml tests/parent-ci-contract.test.ts
git commit -m "ci: gate nested video workbench"
```

### Task 7: Nginx, Deployment Runbook, and Final Acceptance

**Files:**
- Create: `infra/nginx-video-workbench.conf`
- Create: `infra/nginx-video-workbench.test.ts`
- Create: `docs/operations/production-deployment.md`
- Modify: `README.md`

**Interfaces:**
- Produces: Nginx locations `/video-workbench/` and `/api/video-workbench/` proxying only to `127.0.0.1:4173` with SSE buffering disabled.

- [ ] **Step 1: Write failing Nginx contract tests**

```ts
expect(config).toContain('proxy_pass http://127.0.0.1:4173');
expect(config).toContain('proxy_buffering off');
expect(config).not.toMatch(/0\.0\.0\.0/);
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- infra/nginx-video-workbench.test.ts`
Expected: configuration does not exist.

- [ ] **Step 3: Add proxy config and reversible deployment procedure**

Document SSH-key prerequisite, server-side secret generation, preflight backup, migration, staged startup, Nginx syntax check, authenticated smoke test, fixture render, health check, and rollback without deleting volumes.

- [ ] **Step 4: Run full acceptance**

Run: `npm run typecheck && npm run build && npm test && npm run db:verify && npm run fixture && npm run test:e2e && npm audit --omit=dev --audit-level=high`
Expected: all checks pass; fixture reaches `COMPLETED`; E2E passes.

- [ ] **Step 5: Request independent re-review and commit**

```bash
git add infra docs README.md
git commit -m "docs: add secure production deployment"
```

The reviewer must report no Critical or Important findings before the PR is updated or merged.
