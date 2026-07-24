import {describe, expect, it, vi} from 'vitest';
import {InMemoryPipelineRepository, PipelineConfigurationError, PipelineService, Task4PipelineRepository, createOfflineBullMqJobRunner, createSceneJobConsumer, runProjectPipeline, type PipelineDependencies} from './pipeline.js';
import {InMemoryProjectEventBroker, InMemoryProjectRepository, openProjectEventStream} from '@huangque/api';
import {IllegalProjectTransitionError, transitionProject} from './state-transitions.js';
import {isMissingFactualAsset} from './processors/assets.js';
import {isAvatarScene} from './processors/avatar.js';
import {allRequiredScenesReady} from './processors/render.js';
import type {Scene, Storyboard} from '@huangque/contracts';
import {type AvatarProvider, type ImageProvider} from '@huangque/providers';
import {MockAvatarProvider, MockImageProvider} from '@huangque/providers/development';

const board = (scenes: Scene[]): Storyboard => ({
  project: {title: 'fixture', width: 1080, height: 1920, fps: 30},
  scenes
});

const avatarScene = (id = 'scene_001'): Scene => ({
  id, order: 1, type: 'avatar', purpose: 'intro', script: 'Hello', durationEstimate: 2,
  visual: {layout: 'avatar_full', highlightWords: []}
});

const imageScene = (id = 'scene_002', factual = false): Scene => ({
  id, order: 2, type: factual ? 'screenshot' : 'image', purpose: 'visual', script: 'Product', durationEstimate: 2,
  visual: {layout: 'visual_full', highlightWords: []},
  asset: factual ? {source: 'upload', factual: true} : undefined
});

const generated = {uri: 'mock://asset', width: 1080, height: 1920, provenance: 'generated' as const, inputHash: 'hash'};
const passingQualityInspector = {inspect: async () => ({report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/quality.json'})};

const createPipeline = (scenes: Scene[], overrides: Partial<PipelineDependencies> = {}) => {
  const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'CREATED'}]);
  const avatarProvider: AvatarProvider = {generate: async () => generated};
  const imageProvider: ImageProvider = {generate: async () => generated};
  const dependencies: PipelineDependencies = {
    repository,
    queue: {submit: async (job) => ({id: job.id, existing: false})},
    buildStoryboard: () => board(scenes),
    avatarProvider,
    imageProvider,
    qualityInspector: passingQualityInspector,
    ...overrides
  };
  dependencies.jobRunner = dependencies.jobRunner ?? createOfflineBullMqJobRunner(repository, createSceneJobConsumer(dependencies));
  const service = new PipelineService(dependencies);
  return {repository, service};
};

const createRegisteredService = (dependencies: PipelineDependencies): PipelineService => {
  dependencies.qualityInspector ??= passingQualityInspector;
  dependencies.jobRunner = dependencies.jobRunner ?? createOfflineBullMqJobRunner(dependencies.repository, createSceneJobConsumer(dependencies));
  return new PipelineService(dependencies);
};

const runTestPipeline = async (options: {failSceneOnce: string}) => {
  const attempts: Record<string, number> = {};
  const image = new MockImageProvider();
  const imageProvider: ImageProvider = {generate: async (request) => {
    attempts[request.sceneId] = (attempts[request.sceneId] ?? 0) + 1;
    if (request.sceneId === options.failSceneOnce && attempts[request.sceneId] === 1) throw new Error('temporary provider failure');
    return image.generate(request);
  }};
  const avatar = new MockAvatarProvider();
  const avatarProvider: AvatarProvider = {generate: async (request) => {
    attempts[request.sceneId] = (attempts[request.sceneId] ?? 0) + 1;
    return avatar.generate(request);
  }};
  const {repository} = createPipeline([avatarScene(), imageScene()], {avatarProvider, imageProvider});
  let service!: PipelineService;
  const dependencies: PipelineDependencies = {
    repository,
    queue: {submit: async (job) => ({id: job.id, existing: false})},
    buildStoryboard: () => board([avatarScene(), imageScene()]),
    avatarProvider,
    imageProvider
  };
  service = createRegisteredService(dependencies);
  await service.runProjectPipeline('project_001');
  const project = await repository.findProject('project_001');
  const scenes = await repository.listScenes('project_001');
  return {attempts, status: project?.status, assetVersions: Object.fromEntries(scenes.map((scene) => [scene.id, scene.assetVersions]))};
};

describe('runProjectPipeline', () => {
  it('publishes a committed worker status snapshot through the shared API event broker', async () => {
    const durable = new InMemoryProjectRepository();
    await durable.createProject({id: 'project_events', title: 'Events', status: 'CREATED', input: {type: 'script', content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'}, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString()});
    const broker = new InMemoryProjectEventBroker();
    const snapshots: string[] = [];
    broker.subscribe('project_events', (project) => snapshots.push(project.status));
    const repository = new Task4PipelineRepository(durable, () => new Date(), broker);
    await repository.updateProjectStatus('project_events', 'STORYBOARDING');
    expect(snapshots).toEqual(['STORYBOARDING']);
  });

  it('delivers a worker-originated update to an SSE client stream', async () => {
    const durable = new InMemoryProjectRepository(); const now = new Date().toISOString();
    await durable.createProject({id: 'project_sse', title: 'SSE', status: 'CREATED', input: {type: 'script', content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'}, createdAt: now, updatedAt: now});
    const broker = new InMemoryProjectEventBroker(); const writes: string[] = [];
    const initial = await durable.findProject('project_sse');
    openProjectEventStream({raw: {write: (chunk: string) => writes.push(chunk), once: () => undefined}, broker, project: initial!, heartbeatMs: 60_000});
    await new Task4PipelineRepository(durable, () => new Date(), broker).updateProjectStatus('project_sse', 'STORYBOARDING');
    expect(writes.at(-1)).toContain('"status":"STORYBOARDING"');
  });
  it('publishes successful generation and asset versions but not a failed generation claim', async () => {
    const durable = new InMemoryProjectRepository(); const now = new Date().toISOString();
    await durable.createProject({id: 'project_mutations', title: 'Mutations', status: 'GENERATING_ASSETS', input: {type: 'script', content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'}, createdAt: now, updatedAt: now});
    await durable.createScene({id: 'scene_001', projectId: 'project_mutations', order: 1, status: 'PENDING', script: 'Scene', visual: {activeGenerationJobId: 'job_1'}, createdAt: now, updatedAt: now});
    const broker = new InMemoryProjectEventBroker(); const snapshots: any[] = []; broker.subscribe('project_mutations', (p) => snapshots.push(p));
    const repository = new Task4PipelineRepository(durable, () => new Date(), broker);
    expect(await repository.beginSceneGeneration('project_mutations', 'scene_001', 'job_1', 0)).toBe(true);
    expect(await repository.beginSceneGeneration('project_mutations', 'scene_001', 'job_wrong', 0)).toBe(false);
    await repository.addAssetVersion('project_mutations', 'scene_001', {uri: '/asset.mp4', provenance: 'generated', inputHash: 'hash', width: 1080, height: 1920});
    expect(snapshots).toHaveLength(2);
    expect(snapshots[0].scenes[0].status).toBe('GENERATING');
    expect(snapshots[1].assetVersions[0].version).toBe(1);
  });
  it('fails before provider invocation when no registered queue runner is configured', async () => {
    let providerCalls = 0;
    const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'CREATED'}]);
    const service = new PipelineService({repository, queue: {submit: async (job) => ({id: job.id, existing: false})}, buildStoryboard: () => board([imageScene()]),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => { providerCalls += 1; return generated; }}});
    await expect(service.runProjectPipeline('project_001')).rejects.toThrow(PipelineConfigurationError);
    expect(providerCalls).toBe(0);
  });

  it('delegates queued scene generation to a runner without invoking providers in the coordinator', async () => {
    const processed: string[] = [];
    const {repository, service} = createPipeline([imageScene()], {
      jobRunner: {run: async ({data}) => {
        processed.push(data.sceneId);
        await repository.updateSceneStatus(data.projectId, data.sceneId, 'READY');
      }},
      imageProvider: {generate: async () => { throw new Error('coordinator invoked image provider'); }}
    });

    await service.runProjectPipeline('project_001');

    expect(processed).toEqual(['scene_002']);
    expect((await repository.findProject('project_001'))?.status).toBe('COMPLETED');
  });

  it('runs the injected output inspector before durably completing a project', async () => {
    const inspect = vi.fn(async () => ({report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/project_001-quality.json'}));
    const {repository, service} = createPipeline([imageScene()], {
      qualityInspector: {inspect}
    } as unknown as Partial<PipelineDependencies>);

    await service.runProjectPipeline('project_001');

    expect(inspect).toHaveBeenCalledOnce();
    expect((await repository.findProject('project_001'))).toMatchObject({status: 'COMPLETED', qualityReportPath: 'reports/project_001-quality.json'});
  });

  it('durably fails a project when its output quality report fails', async () => {
    const {repository, service} = createPipeline([imageScene()], {
      qualityInspector: {inspect: async () => ({report: {passed: false, errors: ['resolution mismatch'], metrics: {}}, reportPath: 'reports/project_001-quality.json'})}
    } as unknown as Partial<PipelineDependencies>);

    await service.runProjectPipeline('project_001');

    expect((await repository.findProject('project_001'))).toMatchObject({status: 'FAILED', qualityReportPath: 'reports/project_001-quality.json'});
  });
  it('retries only the failed scene and preserves READY scenes', async () => {
    const result = await runTestPipeline({failSceneOnce: 'scene_002'});

    expect(result.attempts.scene_001).toBe(1);
    expect(result.attempts.scene_002).toBe(2);
    expect(result.status).toBe('COMPLETED');
    expect(result.assetVersions.scene_001).toHaveLength(1);
  });

  it('allows only declared project status transitions', () => {
    expect(transitionProject('CREATED', 'STORYBOARDING')).toBe('STORYBOARDING');
    expect(() => transitionProject('CREATED', 'COMPLETED')).toThrow(IllegalProjectTransitionError);
  });

  it('fans out scene jobs concurrently and aligns only after every scene is READY', async () => {
    const events: string[] = [];
    let releaseSecondScene!: () => void;
    const secondScene = new Promise<void>((resolve) => { releaseSecondScene = resolve; });
    const {service} = createPipeline([avatarScene(), imageScene()], {
      queue: {submit: async (job) => {
        events.push(`queued:${job.data.sceneId as string}`);
        return {id: job.id, existing: false};
      }},
      imageProvider: {generate: async () => {
        await secondScene;
        events.push('ready:scene_002');
        return generated;
      }},
      avatarProvider: {generate: async () => {
        events.push('ready:scene_001');
        return generated;
      }},
      onProjectStatusPersisted: ({status}) => { events.push(status); }
    });
    const run = service.runProjectPipeline('project_001');
    await vi.waitFor(() => {
      expect(events).toEqual(expect.arrayContaining(['queued:scene_001', 'queued:scene_002', 'ready:scene_001']));
    });

    expect(events).not.toContain('ALIGNING_TIMELINE');
    releaseSecondScene();
    await run;
    expect(events.indexOf('ALIGNING_TIMELINE')).toBeGreaterThan(events.indexOf('ready:scene_002'));
  });

  it('caps transient retries at three attempts with exponential backoff metadata', async () => {
    const backoffs: number[] = [];
    const {repository, service} = createPipeline([imageScene()], {
      imageProvider: {generate: async () => { throw new Error('network unavailable'); }},
      onRetry: ({delayMs}) => backoffs.push(delayMs)
    });

    await expect(service.runProjectPipeline('project_001')).rejects.toThrow('network unavailable');
    expect(backoffs).toEqual([100, 200]);
    expect((await repository.listScenes('project_001'))[0]?.status).toBe('FAILED');
  });

  it('caps generated-media quality regeneration at two attempts', async () => {
    let attempts = 0;
    const {service} = createPipeline([imageScene()], {
      imageProvider: {generate: async () => {
        attempts += 1;
        return generated;
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });

    await expect(service.runProjectPipeline('project_001')).rejects.toThrow('blurred');
    expect(attempts).toBe(2);
  });

  it('pauses for a missing factual asset without generating a substitute', async () => {
    let generatedCount = 0;
    const {repository, service} = createPipeline([imageScene('scene_002', true)], {
      imageProvider: {generate: async () => {
        generatedCount += 1;
        return generated;
      }}
    });

    await service.runProjectPipeline('project_001');

    expect((await repository.findProject('project_001'))?.status).toBe('NEEDS_USER_INPUT');
    expect(generatedCount).toBe(0);
  });

  it('keeps processor decisions explicit for factual assets, avatar jobs, and fan-in', () => {
    const factual = {...imageScene('scene_003', true), projectId: 'project_001', status: 'PENDING' as const, assetVersions: []};

    expect(isMissingFactualAsset(factual)).toBe(true);
    expect(isAvatarScene({...avatarScene(), projectId: 'project_001', status: 'PENDING', assetVersions: []})).toBe(true);
    expect(allRequiredScenesReady([
      {...factual, status: 'FALLBACK_ACCEPTED'},
      {...avatarScene(), projectId: 'project_001', status: 'READY', assetVersions: [generated]}
    ])).toBe(true);
  });

  it('rebuilds and persists storyboard scenes when recovering from STORYBOARDING', async () => {
    const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'STORYBOARDING'}]);
    let builds = 0;
    const service = createRegisteredService({
      repository,
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => { builds += 1; return board([imageScene()]); },
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated}
    });

    await service.runProjectPipeline('project_001');

    expect(builds).toBe(1);
    expect((await repository.listScenes('project_001'))).toHaveLength(1);
    expect((await repository.findProject('project_001'))?.status).toBe('COMPLETED');
  });

  it('prioritizes NEEDS_USER_INPUT when factual material is missing alongside another scene failure', async () => {
    const {repository, service} = createPipeline([imageScene('scene_001', true), imageScene('scene_002')], {
      imageProvider: {generate: async () => { throw new Error('network unavailable'); }}
    });

    await expect(service.runProjectPipeline('project_001')).resolves.toBeUndefined();

    expect((await repository.findProject('project_001'))?.status).toBe('NEEDS_USER_INPUT');
  });

  it('does not submit an already durably queued scene job during a retry', async () => {
    const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'CREATED'}]);
    const firstQueueJobs: string[] = [];
    const baseDependencies = {
      repository,
      buildStoryboard: () => board([imageScene()]),
      avatarProvider: {generate: async () => generated}
    };
    const firstDependencies: PipelineDependencies = {
      ...baseDependencies,
      queue: {submit: async (job) => { firstQueueJobs.push(job.id); return {id: job.id, existing: false}; }},
      imageProvider: {generate: async () => { throw new Error('network unavailable'); }}
    };
    const firstDeliveryConsumer = createSceneJobConsumer(firstDependencies);
    firstDependencies.jobRunner = {run: async (job) => firstDeliveryConsumer.consume({
      job: await repository.findJob(job.id),
      attemptsMade: 0
    })};
    const first = createRegisteredService(firstDependencies);

    await expect(first.runProjectPipeline('project_001')).rejects.toThrow('network unavailable');
    const retriedQueueJobs: string[] = [];
    const retry = createRegisteredService({
      ...baseDependencies,
      queue: {submit: async (job) => { retriedQueueJobs.push(job.id); return {id: job.id, existing: false}; }},
      imageProvider: {generate: async () => generated}
    });
    await retry.runProjectPipeline('project_001');

    expect(firstQueueJobs).toHaveLength(1);
    expect(retriedQueueJobs).toHaveLength(0);
  });

  it('resumes a project persisted in GENERATING_AVATAR', async () => {
    const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'GENERATING_AVATAR'}]);
    await repository.persistScenes('project_001', [imageScene()]);
    const service = createRegisteredService({
      repository,
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => board([imageScene()]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated}
    });

    await service.runProjectPipeline('project_001');

    expect((await repository.findProject('project_001'))?.status).toBe('COMPLETED');
  });

  it('accepts a supplied factual asset without generating a substitute', async () => {
    const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'CREATED'}]);
    const factualScene = imageScene('scene_001', true);
    await repository.persistScenes('project_001', [factualScene]);
    await repository.addAssetVersion('project_001', 'scene_001', {...generated, provenance: 'uploaded'});
    let imageGenerationCalls = 0;
    const service = createRegisteredService({
      repository,
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => board([factualScene]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        imageGenerationCalls += 1;
        return generated;
      }}
    });

    await service.runProjectPipeline('project_001');

    expect(imageGenerationCalls).toBe(0);
    expect((await repository.listScenes('project_001'))[0]?.status).toBe('READY');
    expect((await repository.findProject('project_001'))?.status).toBe('COMPLETED');
  });

  it('resumes a later persisted stage without transitioning backward', async () => {
    const repository = new InMemoryPipelineRepository([{id: 'project_001', title: 'fixture', script: 'fixture', status: 'QUALITY_CHECK'}]);
    const service = createRegisteredService({
      repository, queue: {submit: async (job) => ({id: job.id, existing: false})}, buildStoryboard: () => board([imageScene()]),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated}
    });

    await service.runProjectPipeline('project_001');

    expect((await repository.findProject('project_001'))?.status).toBe('COMPLETED');
  });
});
