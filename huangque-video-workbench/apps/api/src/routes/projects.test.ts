import {describe, expect, it} from 'vitest';
import {createApp} from '../app.js';
import type {HuangqueIdentity} from '../auth/huangque-auth.js';
import {canonicalInputHash, InMemoryProjectRepository, ProjectService} from '../services/project-service.js';
import {BullMqQueueAdapter, InMemoryQueue, jobKey} from '../queue.js';
import {assetVersions, jobs, projects, scenes} from '../db/schema.js';
import {getTableName} from 'drizzle-orm';

const createTestApp = (identity: HuangqueIdentity = {username: 'alice', role: 'editor'}) => {
  const repository = new InMemoryProjectRepository();
  const queue = new InMemoryQueue();
  const app = createApp({repository, queue, idFactory: () => 'project_001', authenticate: async () => identity});

  return {app, repository, queue};
};

const createPayload = () => ({
  input: {type: 'script', content: '大家好。展示一下产品。点击关注。'},
  avatar: {avatarId: 'mock', voiceId: 'mock'},
  output: {templateId: 'vertical_knowledge_v1'}
});
const authenticateAlice = async (): Promise<HuangqueIdentity> => ({username: 'alice', role: 'editor'});

describe('project routes', () => {
  it('rejects near-1MiB request bodies before allocation reaches application schemas', async () => {
    const {app} = createTestApp();
    const response = await app.inject({
      method: 'POST', url: '/api/projects',
      payload: {...createPayload(), input: {type: 'script', content: 'x'.repeat(900_000)}}
    });
    expect(response.statusCode).toBe(413);
    await app.close();
  });

  it('atomically caps active projects and their initial jobs per owner', async () => {
    const repository = new InMemoryProjectRepository();
    const queue = new InMemoryQueue();
    let sequence = 0;
    const app = createApp({
      repository, queue, idFactory: () => `quota_${++sequence}`, authenticate: authenticateAlice
    });
    const responses = await Promise.all(Array.from({length: 25}, () =>
      app.inject({method: 'POST', url: '/api/projects', payload: createPayload()})));
    expect(responses.filter((response) => response.statusCode === 202)).toHaveLength(10);
    expect(responses.filter((response) => response.statusCode === 429)).toHaveLength(15);
    const activeJobs = (await Promise.all(Array.from({length: sequence}, (_, index) =>
      repository.findProjectForWorker(`quota_${index + 1}`))))
      .filter(Boolean).flatMap((project) => project!.jobs);
    expect(activeJobs).toHaveLength(10);
    await app.close();
  });

  it('requires a valid Huangque session for project routes', async () => {
    const app = createApp({repository: new InMemoryProjectRepository(), queue: new InMemoryQueue(), idFactory: () => 'project_001'});

    const response = await app.inject({method: 'GET', url: '/api/projects/project_001'});

    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual({error: 'unauthorized'});
    await app.close();
  });

  it('does not expose or mutate a project owned by another Huangque user', async () => {
    const repository = new InMemoryProjectRepository();
    const bobQueue = new InMemoryQueue();
    const alice = createApp({
      repository, queue: new InMemoryQueue(), idFactory: () => 'project_001',
      authenticate: async () => ({username: 'alice', role: 'editor'})
    });
    const bob = createApp({
      repository, queue: bobQueue, idFactory: () => 'unused',
      authenticate: async () => ({username: 'bob', role: 'editor'})
    });
    const created = await alice.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const projectId = created.json().id as string;
    await repository.createScene({
      id: 'scene_001', projectId, order: 1, status: 'READY', script: 'original scene', visual: {},
      createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
    });

    for (const request of [
      {method: 'GET' as const, url: `/api/projects/${projectId}`},
      {method: 'PATCH' as const, url: `/api/projects/${projectId}/scenes/scene_001`, payload: {script: 'changed'}},
      {method: 'POST' as const, url: `/api/projects/${projectId}/scenes/scene_001/regenerate`},
      {method: 'POST' as const, url: `/api/projects/${projectId}/render`},
      {method: 'GET' as const, url: `/api/projects/${projectId}/events`}
    ]) {
      expect((await bob.inject(request)).statusCode).toBe(404);
    }
    expect((await repository.findProjectForWorker(projectId))?.scenes).toMatchObject([{id: 'scene_001', script: 'original scene'}]);
    expect(bobQueue.jobs()).toHaveLength(0);
    expect((await alice.inject({method: 'GET', url: `/api/projects/${projectId}`})).json()).toMatchObject({ownerUsername: 'alice'});
    await Promise.all([alice.close(), bob.close()]);
  });

  it('accepts only editable canonical scene fields and preserves orchestration metadata', async () => {
    const {app, repository, queue} = createTestApp();
    await repository.createProject({
      id: 'project_editable', ownerUsername: 'alice', title: 'Editable', status: 'GENERATING_ASSETS',
      input: {type: 'script', content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'},
      createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
    });
    await repository.createScene({
      id: 'scene_001', projectId: 'project_editable', order: 1, status: 'GENERATING', script: 'Original narration',
      visual: {layout: 'visual_full', highlightWords: [], visualPrompt: 'Original direction', contentRevision: 7, activeGenerationJobId: 'generation_001', attempt: 2},
      asset: {source: 'generated', prompt: 'Original asset prompt', factual: false},
      createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
    });

    const edited = await app.inject({
      method: 'PATCH', url: '/api/projects/project_editable/scenes/scene_001',
      payload: {script: 'Edited narration', visualPrompt: 'Edited direction', visual: {headline: 'Edited headline'}}
    });
    const forbidden = await app.inject({
      method: 'PATCH', url: '/api/projects/project_editable/scenes/scene_001',
      payload: {status: 'READY', visual: {activeGenerationJobId: 'attacker-job'}}
    });
    const render = await app.inject({method: 'POST', url: '/api/projects/project_editable/render'});

    expect(edited.statusCode).toBe(200);
    expect(edited.json()).toMatchObject({
      script: 'Edited narration',
      visual: {
        layout: 'visual_full', headline: 'Edited headline', visualPrompt: 'Edited direction',
        contentRevision: 8, activeGenerationJobId: 'generation_001', attempt: 2
      },
      asset: {source: 'generated', prompt: 'Original asset prompt', factual: false}
    });
    expect(forbidden.statusCode).toBe(400);
    expect(render.statusCode).toBe(202);
    expect(queue.jobs().find((job) => job.name === 'project.render')?.data.input).toMatchObject({
      scenes: [{script: 'Edited narration', visual: {visualPrompt: 'Edited direction', activeGenerationJobId: 'generation_001'}}]
    });
    expect((await repository.findProjectForWorker('project_editable'))?.scenes).toMatchObject([{
      script: 'Edited narration', visual: {contentRevision: 8, activeGenerationJobId: 'generation_001', attempt: 2, visualPrompt: 'Edited direction'}
    }]);
    await app.close();
  });

  it('exposes durable scene failure and preview/export metadata in project details', async () => {
    const {app, repository} = createTestApp();
    await repository.createProject({
      id: 'project_metadata', ownerUsername: 'alice', title: 'Metadata', status: 'COMPLETED',
      input: {type: 'script', content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'},
      previewUrl: '/projects/project_metadata/preview.mp4', downloadUrl: '/projects/project_metadata/final.mp4',
      createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
    });
    await repository.createScene({
      id: 'scene_001', projectId: 'project_metadata', order: 1, status: 'FAILED', script: 'Scene', visual: {},
      failureReason: 'Provider timeout', createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
    });

    const response = await app.inject({method: 'GET', url: '/api/projects/project_metadata'});

    expect(response.statusCode).toBe(200);
    expect(response.json()).toMatchObject({
      previewUrl: '/api/projects/project_metadata/output', downloadUrl: '/api/projects/project_metadata/output',
      scenes: [{id: 'scene_001', failureReason: 'Provider timeout'}]
    });
    await app.close();
  });

  it('registers the project SSE endpoint in an offline Fastify application', async () => {
    const {app} = createTestApp();

    expect(app.printRoutes()).toContain('events (GET, HEAD)');
    const missing = await app.inject({method: 'GET', url: '/api/projects/project_missing/events'});
    expect(missing.statusCode).toBe(404);
    await app.close();
  });
  it('creates a project and returns without waiting for generation', async () => {
    const {app, queue} = createTestApp();

    const response = await app.inject({
      method: 'POST',
      url: '/api/projects',
      payload: createPayload()
    });

    expect(response.statusCode).toBe(202);
    expect(response.json()).toMatchObject({id: 'project_001', status: 'CREATED'});
    expect(queue.jobs()).toMatchObject([{name: 'storyboard.generate', id: expect.stringContaining('project_001:project:storyboard.generate:')}]);
  });

  it('keeps the colon-delimited business key in durable project jobs', async () => {
    const {app} = createTestApp();
    const created = await app.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const project = await app.inject({method: 'GET', url: `/api/projects/${created.json().id as string}`});

    expect(project.json()).toMatchObject({jobs: [{id: expect.stringMatching(/^project_001:project:storyboard\.generate:/)}]});
    await app.close();
  });

  it('derives a deterministic colon-free BullMQ id from the business key', async () => {
    const jobIds: string[] = [];
    const queue = new BullMqQueueAdapter({
      add: async (_name, _data, options) => {
        jobIds.push(options.jobId);
        return {id: options.jobId};
      }
    });
    const key = jobKey('project_001', 'scene_001', 'scene.regenerate', 'input_hash');

    const first = await queue.submit({id: key, name: 'scene.regenerate', data: {projectId: 'project_001'}});
    const second = await new BullMqQueueAdapter({
      add: async (_name, _data, options) => {
        jobIds.push(options.jobId);
        return {id: options.jobId};
      }
    }).submit({id: key, name: 'scene.regenerate', data: {projectId: 'project_001'}});

    expect(first).toMatchObject({id: key});
    expect(second).toMatchObject({id: key});
    expect(jobIds).toHaveLength(2);
    expect(jobIds[0]).toMatch(/^[a-f0-9]{64}$/);
    expect(jobIds[0]).not.toContain(':');
    expect(jobIds[1]).toBe(jobIds[0]);
  });

  it('uses canonical SHA-256 hashes for equivalent and distinct job inputs', () => {
    const first = canonicalInputHash({output: {templateId: 'vertical'}, scenes: ['scene_001']});
    const reordered = canonicalInputHash({scenes: ['scene_001'], output: {templateId: 'vertical'}});
    const changed = canonicalInputHash({scenes: ['scene_002'], output: {templateId: 'vertical'}});

    expect(first).toMatch(/^[a-f0-9]{64}$/);
    expect(reordered).toBe(first);
    expect(changed).not.toBe(first);
  });

  it('returns the existing identifier when a job is submitted twice', async () => {
    const {app, queue} = createTestApp();
    const project = await app.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const id = project.json().id as string;

    const first = await app.inject({method: 'POST', url: `/api/projects/${id}/render`});
    const second = await app.inject({method: 'POST', url: `/api/projects/${id}/render`});

    expect(first.statusCode).toBe(202);
    expect(second.statusCode).toBe(202);
    expect(second.json()).toMatchObject({jobId: first.json().jobId, existing: true});
    expect(queue.jobs().filter((job) => job.name === 'project.render')).toHaveLength(1);
  });

  it('does not regress running or terminal durable render jobs back to QUEUED', async () => {
    const {app, queue, repository} = createTestApp();
    const created = await app.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const projectId = created.json().id as string;
    const first = await app.inject({method: 'POST', url: `/api/projects/${projectId}/render`});
    const jobId = first.json().jobId as string;

    await repository.claimRenderJobLease(jobId, 'running-owner', 10_000);
    const runningDuplicate = await app.inject({method: 'POST', url: `/api/projects/${projectId}/render`});
    expect(runningDuplicate.json()).toMatchObject({jobId, existing: true});
    expect((await repository.findJob(jobId))?.status).toBe('RUNNING');

    await repository.commitRenderJobTerminal({projectId, jobId, owner: 'running-owner', status: 'CANCELLED'});
    const terminalDuplicate = await app.inject({method: 'POST', url: `/api/projects/${projectId}/render`});
    expect(terminalDuplicate.json()).toMatchObject({jobId, existing: true});
    expect((await repository.findJob(jobId))?.status).toBe('CANCELLED');
    expect(queue.jobs().filter((job) => job.name === 'project.render')).toHaveLength(1);
    await app.close();
  });

  it('returns the same durable render job to concurrent enqueue callers', async () => {
    const {app, queue} = createTestApp();
    const created = await app.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const projectId = created.json().id as string;

    const [first, second] = await Promise.all([
      app.inject({method: 'POST', url: `/api/projects/${projectId}/render`}),
      app.inject({method: 'POST', url: `/api/projects/${projectId}/render`})
    ]);

    expect(first.statusCode).toBe(202);
    expect(second.statusCode).toBe(202);
    expect(second.json().jobId).toBe(first.json().jobId);
    expect([first.json().existing, second.json().existing]).toContain(true);
    expect(queue.jobs().filter((job) => job.name === 'project.render')).toHaveLength(1);
    await app.close();
  });

  it('uses persisted job state for a duplicate submitted by a fresh queue adapter', async () => {
    const repository = new InMemoryProjectRepository();
    const firstQueueCalls: string[] = [];
    const app = createApp({
      repository,
      queue: new BullMqQueueAdapter({add: async (_name, _data, options) => {
        firstQueueCalls.push(options.jobId);
        return {id: options.jobId};
      }}),
      idFactory: () => 'project_001', authenticate: authenticateAlice
    });
    const project = await app.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const id = project.json().id as string;
    const first = await app.inject({method: 'POST', url: `/api/projects/${id}/render`});
    const freshQueueCalls: string[] = [];
    const freshApp = createApp({
      repository,
      queue: new BullMqQueueAdapter({add: async (_name, _data, options) => {
        freshQueueCalls.push(options.jobId);
        return {id: options.jobId};
      }}),
      idFactory: () => 'unused', authenticate: authenticateAlice
    });

    const second = await freshApp.inject({method: 'POST', url: `/api/projects/${id}/render`});

    expect(second.json()).toMatchObject({jobId: first.json().jobId, existing: true});
    expect(firstQueueCalls).toHaveLength(2);
    expect(freshQueueCalls).toHaveLength(0);
    await Promise.all([app.close(), freshApp.close()]);
  });

  it('retries a persisted pending job after queue submission fails', async () => {
    const {app, repository} = createTestApp();
    const project = await app.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const id = project.json().id as string;
    const failingApp = createApp({repository, queue: {submit: async () => { throw new Error('queue unavailable'); }}, idFactory: () => 'unused', authenticate: authenticateAlice});

    expect((await failingApp.inject({method: 'POST', url: `/api/projects/${id}/render`})).statusCode).toBe(500);
    const recoveredQueue = new InMemoryQueue();
    const recoveredApp = createApp({repository, queue: recoveredQueue, idFactory: () => 'unused', authenticate: authenticateAlice});
    const retry = await recoveredApp.inject({method: 'POST', url: `/api/projects/${id}/render`});

    expect(retry).toMatchObject({statusCode: 202});
    expect(retry.json()).toMatchObject({existing: false});
    expect(recoveredQueue.jobs()).toHaveLength(1);
    await Promise.all([app.close(), failingApp.close(), recoveredApp.close()]);
  });

  it('keeps a created project and pending storyboard job when its initial enqueue fails', async () => {
    const repository = new InMemoryProjectRepository();
    const failingApp = createApp({repository, queue: {submit: async () => { throw new Error('queue unavailable'); }}, idFactory: () => 'project_001', authenticate: authenticateAlice});

    const created = await failingApp.inject({method: 'POST', url: '/api/projects', payload: createPayload()});
    const persisted = await failingApp.inject({method: 'GET', url: '/api/projects/project_001'});

    expect(created).toMatchObject({statusCode: 202});
    expect(persisted.json()).toMatchObject({status: 'CREATED', jobs: [{status: 'PENDING', taskType: 'storyboard.generate'}]});
    const recoveredQueue = new InMemoryQueue();
    const recoveredService = new ProjectService(repository, recoveredQueue, () => 'unused');
    await recoveredService.retryStoryboard('project_001');
    expect(recoveredQueue.jobs()).toHaveLength(1);
    await Promise.all([failingApp.close()]);
  });

  it('leaves duplicate status to durable reservation rather than an adapter-local set', async () => {
    const options: string[] = [];
    const queue = new BullMqQueueAdapter({add: async (_name, _data, option) => {
      options.push(option.jobId);
      return {id: option.jobId};
    }});
    const job = {id: jobKey('p', 's', 'render', 'hash'), name: 'render', data: {projectId: 'p'}};

    expect(await queue.submit(job)).toMatchObject({id: job.id, existing: false});
    expect(await queue.submit(job)).toMatchObject({id: job.id, existing: false});
    expect(options).toHaveLength(2);
  });

  it('validates payloads and route ids with structured errors', async () => {
    const {app} = createTestApp();

    const invalidPayload = await app.inject({method: 'POST', url: '/api/projects', payload: {input: {type: 'script'}}});
    const invalidId = await app.inject({method: 'GET', url: '/api/projects/not a valid id'});
    const missing = await app.inject({method: 'GET', url: '/api/projects/project_missing'});

    expect(invalidPayload.statusCode).toBe(400);
    expect(invalidPayload.json()).toMatchObject({error: 'validation_error'});
    expect(invalidId.statusCode).toBe(400);
    expect(invalidId.json()).toMatchObject({error: 'validation_error'});
    expect(missing.statusCode).toBe(404);
    expect(missing.json()).toMatchObject({error: 'not_found'});
  });

  it('returns a structured error for malformed encoded ids', async () => {
    const {app} = createTestApp();

    const response = await app.inject({method: 'GET', url: '/api/projects/%'});

    expect(response.statusCode).toBe(400);
    expect(response.json()).toMatchObject({error: 'validation_error'});
  });

  it('builds a real Fastify application for offline injection', async () => {
    const {app} = createTestApp();

    expect(typeof app.close).toBe('function');
    await expect(app.inject({method: 'POST', url: '/api/projects', payload: createPayload()})).resolves.toMatchObject({statusCode: 202});
    await app.close();
  });

  it('defines real Drizzle metadata tables', () => {
    expect([projects, scenes, jobs, assetVersions].map(getTableName)).toEqual(['projects', 'scenes', 'jobs', 'asset_versions']);
  });
});
