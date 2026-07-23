# Huangque Video Workbench MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a testable vertical MVP that accepts a Chinese script, produces a structured mixed avatar/B-roll storyboard, renders a 9:16 captioned preview, and exposes project progress plus scene-level regeneration.

**Architecture:** Use an npm-workspaces TypeScript monorepo with a Fastify API, BullMQ workers, PostgreSQL metadata, S3-compatible object storage, and a Remotion renderer. External avatar/image/video systems sit behind provider interfaces; deterministic mock providers complete the first end-to-end milestone before real credentials are introduced.

**Tech Stack:** Node.js 22.13+, TypeScript 5.9, Fastify, Zod, BullMQ, Redis, PostgreSQL, Drizzle ORM, Remotion, React, FFmpeg/ffprobe, Vitest, Playwright, Docker Compose.

## Global Constraints

- Output is 1080?1920, 30 fps, H.264 video with AAC audio; preview may render at 540?960.
- The final audio/video duration difference must not exceed 100 ms.
- Avatar scenes must not exceed 12 seconds; normal B-roll scenes target 3?6 seconds.
- User uploads outrank enterprise assets; enterprise assets outrank generated assets.
- Certificates, news, product facts, and numeric claims may not be replaced by invented AI media.
- All external media providers are accessed through adapters; domain code may not import vendor SDKs.
- Every mutation job uses `projectId:sceneId:taskType:inputHash` as its idempotency key.
- No task may require the production server or production credentials to pass tests.

---

## Planned File Structure

```text
apps/
??? api/src/                 Fastify routes and project orchestration
??? web/src/                 Project creation, storyboard, preview, export UI
??? worker/src/              BullMQ processors and pipeline coordination
packages/
??? contracts/src/           Zod schemas and shared TypeScript types
??? director/src/            Script segmentation and storyboard decisions
??? providers/src/           Avatar, image, video, ASR, storage adapters
??? timeline/src/            Millisecond/frame alignment and validation
??? renderer/src/            Remotion composition, scenes, captions, themes
??? media/src/               FFmpeg normalization, probing, and quality checks
tests/fixtures/              Licensed/generated test media only
infra/docker-compose.yml     PostgreSQL, Redis, and MinIO for local development
```

## Phase 1 ? Executable Vertical MVP

### Task 1: Workspace, contracts, and local infrastructure

**Files:**
- Create: `package.json`
- Create: `tsconfig.base.json`
- Create: `vitest.workspace.ts`
- Create: `infra/docker-compose.yml`
- Create: `packages/contracts/package.json`
- Create: `packages/contracts/src/storyboard.ts`
- Create: `packages/contracts/src/project.ts`
- Test: `packages/contracts/src/storyboard.test.ts`

**Interfaces:**
- Produces: `StoryboardSchema`, `Storyboard`, `SceneSchema`, `Scene`, `ProjectStatusSchema`, `ProjectStatus`.

- [ ] **Step 1: Create the workspace manifests and failing schema test**

```json
{
  "name": "huangque-video-workbench",
  "private": true,
  "engines": {"node": ">=22.13.0"},
  "workspaces": ["apps/*", "packages/*"],
  "scripts": {
    "test": "vitest run",
    "typecheck": "tsc -b",
    "infra:up": "docker compose -f infra/docker-compose.yml up -d"
  },
  "devDependencies": {"typescript": "5.9.3", "vitest": "^4.1.10", "zod": "^4.0.0"}
}
```

```ts
// packages/contracts/src/storyboard.test.ts
import {describe, expect, it} from 'vitest';
import {StoryboardSchema} from './storyboard';

describe('StoryboardSchema', () => {
  it('rejects an avatar scene longer than 12 seconds', () => {
    const result = StoryboardSchema.safeParse({
      project: {title: '??', width: 1080, height: 1920, fps: 30},
      scenes: [{id: 's1', order: 1, type: 'avatar', purpose: 'intro', script: '??', durationEstimate: 13, visual: {layout: 'avatar_full', highlightWords: []}}]
    });
    expect(result.success).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `npm install && npm test -- packages/contracts/src/storyboard.test.ts`

Expected: FAIL because `./storyboard` does not exist.

- [ ] **Step 3: Implement the shared schemas**

```ts
// packages/contracts/src/storyboard.ts
import {z} from 'zod';

export const SceneTypeSchema = z.enum(['avatar', 'image', 'image_video', 'stock_video', 'upload', 'chart', 'screenshot', 'text_fallback']);
export const SceneSchema = z.object({
  id: z.string().min(1), order: z.number().int().positive(), type: SceneTypeSchema,
  purpose: z.string().min(1), script: z.string().min(1), durationEstimate: z.number().positive(),
  visual: z.object({layout: z.string().min(1), headline: z.string().nullable().optional(), highlightWords: z.array(z.string())}),
  asset: z.object({source: z.string(), query: z.string().optional(), prompt: z.string().optional(), factual: z.boolean().default(false)}).nullable().optional()
}).superRefine((scene, ctx) => {
  if (scene.type === 'avatar' && scene.durationEstimate > 12) ctx.addIssue({code: 'custom', message: 'avatar scene exceeds 12 seconds'});
});
export const StoryboardSchema = z.object({
  project: z.object({title: z.string(), width: z.literal(1080), height: z.literal(1920), fps: z.literal(30)}),
  scenes: z.array(SceneSchema).min(1)
});
export type Scene = z.infer<typeof SceneSchema>;
export type Storyboard = z.infer<typeof StoryboardSchema>;
```

```ts
// packages/contracts/src/project.ts
import {z} from 'zod';
export const ProjectStatusSchema = z.enum(['CREATED','STORYBOARDING','GENERATING_ASSETS','GENERATING_AVATAR','ALIGNING_TIMELINE','RENDERING','QUALITY_CHECK','COMPLETED','RETRYING','PARTIALLY_FAILED','NEEDS_USER_INPUT','FAILED','CANCELLED']);
export type ProjectStatus = z.infer<typeof ProjectStatusSchema>;
```

- [ ] **Step 4: Add PostgreSQL, Redis, and MinIO to Docker Compose and verify services**

```yaml
services:
  postgres:
    image: postgres:17-alpine
    environment: {POSTGRES_USER: huangque, POSTGRES_PASSWORD: localdev, POSTGRES_DB: huangque}
    ports: ["5432:5432"]
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  minio:
    image: minio/minio
    command: server /data --console-address :9001
    environment: {MINIO_ROOT_USER: localdev, MINIO_ROOT_PASSWORD: localdevsecret}
    ports: ["9000:9000", "9001:9001"]
```

Run: `npm run infra:up && docker compose -f infra/docker-compose.yml ps`

Expected: all three services show `Up`.

- [ ] **Step 5: Run tests and commit**

Run: `npm test -- packages/contracts/src/storyboard.test.ts`

Expected: PASS.

```bash
git add package.json package-lock.json tsconfig.base.json vitest.workspace.ts infra packages/contracts
git commit -m "chore: scaffold workbench contracts and infrastructure"
```

### Task 2: Deterministic director and storyboard validator

**Files:**
- Create: `packages/director/package.json`
- Create: `packages/director/src/segment-script.ts`
- Create: `packages/director/src/classify-segment.ts`
- Create: `packages/director/src/build-storyboard.ts`
- Create: `packages/director/src/validate-storyboard.ts`
- Test: `packages/director/src/build-storyboard.test.ts`

**Interfaces:**
- Consumes: `Storyboard`, `Scene` from `@huangque/contracts`.
- Produces: `buildStoryboard(input: {title: string; script: string}): Storyboard` and `validateStoryboard(board: Storyboard): string[]`.

- [ ] **Step 1: Write a failing mixed-storyboard test**

```ts
import {describe, expect, it} from 'vitest';
import {buildStoryboard} from './build-storyboard';

it('uses avatar for intro/transition/CTA and visual media for product demonstration', () => {
  const board = buildStoryboard({title: '????', script: '????????????????????????????????????????????????'});
  expect(board.scenes.map((scene) => scene.type)).toEqual(['avatar','avatar','image_video','avatar','avatar']);
  expect(board.scenes.every((scene) => scene.type !== 'avatar' || scene.durationEstimate <= 12)).toBe(true);
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npm test -- packages/director/src/build-storyboard.test.ts`

Expected: FAIL because `build-storyboard.ts` is missing.

- [ ] **Step 3: Implement deterministic segmentation and classification**

```ts
// packages/director/src/segment-script.ts
export const segmentScript = (script: string): string[] => script.split(/(?<=[???!?])/u).map((s) => s.trim()).filter(Boolean);
```

```ts
// packages/director/src/classify-segment.ts
import type {Scene} from '@huangque/contracts';
export const classifySegment = (text: string, index: number, total: number): Scene['type'] => {
  if (index === 0 || index === total - 1) return 'avatar';
  if (/??|??|??|??|????/u.test(text)) return 'image_video';
  if (/??|??|??|??|??/u.test(text)) return 'chart';
  return 'avatar';
};
```

Implement `buildStoryboard` by mapping segments, estimating `Math.max(2, text.length / 4.2)` seconds, splitting avatar segments that exceed 12 seconds, assigning stable `scene_001` IDs, and parsing the result through `StoryboardSchema`.

- [ ] **Step 4: Enforce factual-asset and visual-rhythm invariants**

`validateStoryboard` must return errors when a factual `screenshot`/`upload` scene requests generated media, when an avatar exceeds 12 seconds, or when cumulative scenes exceed 15 seconds without a visual-type scene.

- [ ] **Step 5: Run tests and commit**

Run: `npm test -- packages/director`

Expected: PASS.

```bash
git add packages/director
git commit -m "feat: add deterministic mixed-scene director"
```

### Task 3: Provider contracts, mock assets, and media provenance

**Files:**
- Create: `packages/providers/src/types.ts`
- Create: `packages/providers/src/mock/avatar.ts`
- Create: `packages/providers/src/mock/image.ts`
- Create: `packages/providers/src/mock/video.ts`
- Create: `packages/providers/src/provenance.ts`
- Test: `packages/providers/src/providers.test.ts`
- Create: `tests/fixtures/avatar-source.mp4`
- Create: `tests/fixtures/product.jpg`

**Interfaces:**
- Produces: `AvatarProvider.generate(request): Promise<GeneratedAsset>`, `ImageProvider.generate(request): Promise<GeneratedAsset>`, `VideoProvider.generate(request): Promise<GeneratedAsset>`, and `assertAllowedProvenance(scene, asset): void`.

- [ ] **Step 1: Write failing provider contract tests**

```ts
it('rejects generated media for factual evidence scenes', () => {
  expect(() => assertAllowedProvenance(
    {type: 'screenshot', asset: {source: 'generate', factual: true}},
    {uri: 'mock://image', provenance: 'generated'}
  )).toThrow('factual scenes require uploaded or verified assets');
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npm test -- packages/providers/src/providers.test.ts`

Expected: FAIL because provider modules are missing.

- [ ] **Step 3: Implement interfaces and deterministic mocks**

```ts
export type GeneratedAsset = {uri: string; durationMs?: number; width: number; height: number; provenance: 'uploaded'|'enterprise'|'licensed'|'generated'|'fallback'; inputHash: string};
export type AvatarRequest = {sceneId: string; text: string; audioUri?: string; width: number; height: number};
export interface AvatarProvider { generate(request: AvatarRequest): Promise<GeneratedAsset>; }
```

Mocks copy fixture media to a project-scoped temp directory and return a SHA-256 input hash. They may not use network calls.

- [ ] **Step 4: Run tests and commit**

Run: `npm test -- packages/providers`

Expected: PASS.

```bash
git add packages/providers tests/fixtures
git commit -m "feat: define media providers and provenance rules"
```

### Task 4: Project API, persistence, and idempotent queue submission

**Files:**
- Create: `apps/api/src/app.ts`
- Create: `apps/api/src/routes/projects.ts`
- Create: `apps/api/src/services/project-service.ts`
- Create: `apps/api/src/db/schema.ts`
- Create: `apps/api/src/queue.ts`
- Test: `apps/api/src/routes/projects.test.ts`

**Interfaces:**
- Consumes: `buildStoryboard`, `ProjectStatus`.
- Produces: `POST /api/projects`, `GET /api/projects/:id`, `PATCH /api/projects/:id/scenes/:sceneId`, `POST /api/projects/:id/scenes/:sceneId/regenerate`, `POST /api/projects/:id/render`.

- [ ] **Step 1: Write failing API tests**

```ts
it('creates a project and returns without waiting for generation', async () => {
  const response = await app.inject({method: 'POST', url: '/api/projects', payload: {input: {type: 'script', content: '????????????????'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}}});
  expect(response.statusCode).toBe(202);
  expect(response.json()).toMatchObject({status: 'CREATED'});
});
```

- [ ] **Step 2: Run test and verify failure**

Run: `npm test -- apps/api/src/routes/projects.test.ts`

Expected: FAIL because the API app is missing.

- [ ] **Step 3: Implement database schema and routes**

Create `projects`, `scenes`, `jobs`, and `asset_versions` tables. `POST /api/projects` validates input with Zod, inserts the project transactionally, enqueues `storyboard.generate`, and returns HTTP 202.

- [ ] **Step 4: Implement idempotency**

```ts
export const jobKey = (projectId: string, sceneId: string, taskType: string, inputHash: string) => `${projectId}:${sceneId}:${taskType}:${inputHash}`;
```

Queue submission must use this value as BullMQ `jobId`; duplicate submissions return the existing job identifier.

- [ ] **Step 5: Run API tests and commit**

Run: `npm test -- apps/api`

Expected: PASS.

```bash
git add apps/api
git commit -m "feat: add project API and idempotent job submission"
```

### Task 5: Pipeline workers and scene-level retry

**Files:**
- Create: `apps/worker/src/pipeline.ts`
- Create: `apps/worker/src/processors/storyboard.ts`
- Create: `apps/worker/src/processors/assets.ts`
- Create: `apps/worker/src/processors/avatar.ts`
- Create: `apps/worker/src/processors/render.ts`
- Create: `apps/worker/src/state-transitions.ts`
- Test: `apps/worker/src/pipeline.test.ts`

**Interfaces:**
- Consumes: project records, storyboard, provider interfaces, queue job keys.
- Produces: `runProjectPipeline(projectId: string): Promise<void>` and legal status transitions.

- [ ] **Step 1: Write a failing partial-retry test**

```ts
it('retries only the failed scene and preserves READY scenes', async () => {
  const result = await runTestPipeline({failSceneOnce: 'scene_002'});
  expect(result.attempts.scene_001).toBe(1);
  expect(result.attempts.scene_002).toBe(2);
  expect(result.status).toBe('COMPLETED');
});
```

- [ ] **Step 2: Run test and verify failure**

Run: `npm test -- apps/worker/src/pipeline.test.ts`

Expected: FAIL because the pipeline does not exist.

- [ ] **Step 3: Implement legal transitions and fan-out/fan-in**

The storyboard processor persists scenes, then enqueues one provider job per scene. Avatar and asset queues run concurrently. `ALIGNING_TIMELINE` begins only when every required scene is `READY` or has an accepted fallback.

- [ ] **Step 4: Implement retry and user-input decisions**

Network failures retry three times with exponential backoff. Generated media quality failures regenerate twice. Missing factual assets transition the project to `NEEDS_USER_INPUT`. Successful scene asset versions remain untouched.

- [ ] **Step 5: Run tests and commit**

Run: `npm test -- apps/worker`

Expected: PASS.

```bash
git add apps/worker
git commit -m "feat: orchestrate scene generation with partial retry"
```

### Task 6: Timeline alignment and caption model

**Files:**
- Create: `packages/timeline/src/build-timeline.ts`
- Create: `packages/timeline/src/validate-timeline.ts`
- Create: `packages/timeline/src/captions.ts`
- Test: `packages/timeline/src/build-timeline.test.ts`

**Interfaces:**
- Produces: `buildTimeline(input: TimelineInput): Timeline`, `validateTimeline(timeline): string[]`, and `activeCaptionAtFrame(words, frame, fps)`.

- [ ] **Step 1: Write failing frame-accuracy tests**

```ts
it('uses exact audio duration and contiguous frames', () => {
  const timeline = buildTimeline({fps: 30, scenes: [{id: 's1', audioDurationMs: 3210}, {id: 's2', audioDurationMs: 1990}]});
  expect(timeline.scenes).toEqual([
    {id: 's1', startFrame: 0, durationInFrames: 97, endFrame: 97},
    {id: 's2', startFrame: 97, durationInFrames: 60, endFrame: 157}
  ]);
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npm test -- packages/timeline`

Expected: FAIL because timeline modules are missing.

- [ ] **Step 3: Implement frame conversion, captions, and validation**

Use `Math.ceil(milliseconds / 1000 * fps)`. Validation rejects gaps, overlaps, non-positive durations, unsupported fps, and caption timestamps beyond their scene.

- [ ] **Step 4: Run tests and commit**

Run: `npm test -- packages/timeline`

Expected: PASS.

```bash
git add packages/timeline
git commit -m "feat: align audio scenes and word captions to frames"
```

### Task 7: Remotion vertical knowledge template

**Files:**
- Create: `packages/renderer/src/Root.tsx`
- Create: `packages/renderer/src/VerticalKnowledgeVideo.tsx`
- Create: `packages/renderer/src/scenes/AvatarFullScene.tsx`
- Create: `packages/renderer/src/scenes/AvatarTitleScene.tsx`
- Create: `packages/renderer/src/scenes/AvatarPipScene.tsx`
- Create: `packages/renderer/src/scenes/VisualFullScene.tsx`
- Create: `packages/renderer/src/scenes/VisualCardScene.tsx`
- Create: `packages/renderer/src/scenes/ComparisonScene.tsx`
- Create: `packages/renderer/src/scenes/TextFallbackScene.tsx`
- Create: `packages/renderer/src/components/Captions.tsx`
- Create: `packages/renderer/src/themes/knowledge.ts`
- Test: `packages/renderer/src/VerticalKnowledgeVideo.test.tsx`

**Interfaces:**
- Consumes: `Timeline`, scene asset URIs, caption words, theme.
- Produces: Remotion composition `VerticalKnowledgeVideo` and `calculateMetadata` with exact total frames.

- [ ] **Step 1: Write failing composition tests**

```tsx
it('maps every timeline scene to exactly one sequence', () => {
  const tree = render(<VerticalKnowledgeVideo {...fixtureProps}/>);
  expect(tree.getAllByTestId(/^scene-/)).toHaveLength(fixtureProps.timeline.scenes.length);
});
```

- [ ] **Step 2: Run test and verify failure**

Run: `npm test -- packages/renderer`

Expected: FAIL because the composition is missing.

- [ ] **Step 3: Implement layouts and theme contracts**

Use `<Sequence from={startFrame} durationInFrames={durationInFrames}>` for each scene. Apply a 60 px horizontal safe area, title Y range 140?380, caption Y range 1320?1640, and a maximum of two caption lines.

- [ ] **Step 4: Implement word-highlight captions**

The caption component chooses the active phrase using the current frame. The currently spoken word uses the primary theme color; director highlight words use weight 800; all other words remain white on a 52% black background.

- [ ] **Step 5: Render a fixture preview and commit**

Run: `npx remotion render VerticalKnowledgeVideo tests/output/template-preview.mp4 --props=tests/fixtures/project.json --scale=0.5`

Expected: a 540?960 preview with alternating avatar and visual scenes, no clipped captions, and exact fixture duration.

```bash
git add packages/renderer
git commit -m "feat: render vertical knowledge video template"
```

### Task 8: FFmpeg normalization and automated quality report

**Files:**
- Create: `packages/media/src/probe.ts`
- Create: `packages/media/src/normalize-video.ts`
- Create: `packages/media/src/normalize-audio.ts`
- Create: `packages/media/src/quality-report.ts`
- Test: `packages/media/src/quality-report.test.ts`

**Interfaces:**
- Produces: `probeMedia(path): Promise<MediaProbe>`, `normalizeVideo(input, output): Promise<void>`, and `inspectOutput(path, expected): Promise<QualityReport>`.

- [ ] **Step 1: Write a failing quality-report test**

```ts
it('fails output with the wrong resolution or duration drift above 100ms', async () => {
  const report = await inspectOutput('tests/fixtures/wrong-size.mp4', {width: 1080, height: 1920, durationMs: 5000});
  expect(report.passed).toBe(false);
  expect(report.errors).toContain('resolution mismatch');
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npm test -- packages/media`

Expected: FAIL because media inspection is missing.

- [ ] **Step 3: Implement ffprobe parsing and normalization commands**

Video normalization must use `scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30`; audio normalization must use `loudnorm=I=-16:LRA=11:TP=-1.5`.

- [ ] **Step 4: Implement output checks**

Check codec, resolution, audio presence, duration drift, black-frame ratio, and unexpected silence. Emit `reports/quality.json`; failed reports prevent `COMPLETED`.

- [ ] **Step 5: Run tests and commit**

Run: `npm test -- packages/media`

Expected: PASS.

```bash
git add packages/media
git commit -m "feat: normalize media and enforce output quality"
```

### Task 9: Minimal workbench UI

**Files:**
- Create: `apps/web/src/routes/NewProject.tsx`
- Create: `apps/web/src/routes/ProjectDetail.tsx`
- Create: `apps/web/src/components/StoryboardCard.tsx`
- Create: `apps/web/src/components/ProjectProgress.tsx`
- Create: `apps/web/src/components/VideoPreview.tsx`
- Create: `apps/web/src/api/client.ts`
- Test: `apps/web/src/routes/ProjectDetail.test.tsx`

**Interfaces:**
- Consumes: project API endpoints and project/scene contracts.
- Produces: create-project form, storyboard card editor, progress view, preview, scene regenerate, and final download controls.

- [ ] **Step 1: Write failing project-detail UI tests**

```tsx
it('regenerates one scene without submitting the full project', async () => {
  render(<ProjectDetail project={fixtureProject}/>);
  await user.click(screen.getByRole('button', {name: '???? scene_002'}));
  expect(api.regenerateScene).toHaveBeenCalledWith(fixtureProject.id, 'scene_002');
  expect(api.renderProject).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npm test -- apps/web`

Expected: FAIL because UI modules are missing.

- [ ] **Step 3: Implement the create and project-detail screens**

The create form requires script, avatar, voice, and template. Project detail shows ordered scene cards, lock state, asset version, status, failure reason, and per-scene regeneration. Do not add a free-form timeline.

- [ ] **Step 4: Add SSE progress with polling fallback**

Connect to `/api/projects/:id/events`; if SSE disconnects twice, poll `GET /api/projects/:id` every five seconds until a terminal state.

- [ ] **Step 5: Run tests and commit**

Run: `npm test -- apps/web`

Expected: PASS.

```bash
git add apps/web
git commit -m "feat: add storyboard review and preview workbench"
```

### Task 10: End-to-end fixture pipeline and operator documentation

**Files:**
- Create: `tests/e2e/create-project.spec.ts`
- Create: `scripts/run-fixture-project.ts`
- Create: `.env.example`
- Create: `README.md`
- Create: `docs/operations/local-development.md`

**Interfaces:**
- Consumes: all prior packages and services.
- Produces: one-command local fixture render and a browser-level acceptance test.

- [ ] **Step 1: Write the failing end-to-end test**

```ts
test('script becomes a completed mixed-scene preview', async ({page}) => {
  await page.goto('/projects/new');
  await page.getByLabel('????').fill('??????????????????????????????????????');
  await page.getByRole('button', {name: '????'}).click();
  await expect(page.getByText('COMPLETED')).toBeVisible({timeout: 120_000});
  await expect(page.locator('video')).toHaveAttribute('src', /preview\.mp4$/);
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npx playwright test tests/e2e/create-project.spec.ts`

Expected: FAIL before the full stack is started or while the missing fixture pipeline is incomplete.

- [ ] **Step 3: Implement the fixture runner and documentation**

`scripts/run-fixture-project.ts` starts a project through the HTTP API and waits until a terminal state. `.env.example` lists only local values and variable names; it must contain no production server address, password, token, or personal data.

- [ ] **Step 4: Run complete verification**

Run:

```bash
npm run infra:up
npm run typecheck
npm test
npx playwright test tests/e2e/create-project.spec.ts
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration -of json tests/output/final.mp4
```

Expected: typecheck and all tests pass; ffprobe reports H.264, AAC, 1080?1920, and the expected fixture duration within 100 ms.

- [ ] **Step 5: Commit**

```bash
git add tests scripts .env.example README.md docs/operations
git commit -m "test: verify one-click mixed video pipeline"
```

## Phase 2 ? Real Provider Integration

After Phase 1 passes without production credentials, create separate provider-specific plans. Each plan must cover authentication, request validation, callback verification, rate limiting, cost accounting, content moderation, cancellation, data retention, and a mock-backed contract test. Recommended order:

1. Production object storage.
2. Chosen avatar provider.
3. Chosen image provider.
4. Chosen image-to-video provider.
5. Production ASR/TTS or voice-clone provider.

Production deployment is a separate plan because it requires an explicit decision about domain, TLS, firewall, secret storage, database backup, object-storage retention, monitoring, and whether the supplied server will host rendering workloads or only the API/control plane.
