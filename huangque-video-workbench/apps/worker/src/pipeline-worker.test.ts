import {describe, expect, it} from 'vitest';
import {UnrecoverableError} from 'bullmq';
import type {ProjectStatus, Scene, Storyboard} from '@huangque/contracts';
import {canonicalInputHash, InMemoryProjectRepository, OutboxDispatcher, type ProjectRecord, type QueueAdapter, type QueueJob} from '@huangque/api';
import {ProjectService} from '../../api/src/services/project-service.js';
import {Task4PipelineRepository} from './pipeline.js';
import {createBullMqPipelineProcessor as createProductionProcessor, createProductionWorkerComposition, type BullMqSceneDelivery} from './bullmq-worker.js';
import {InMemoryProjectEventBroker, openProjectEventStream} from '@huangque/api';

const generated = {uri: 'mock://asset', width: 1080, height: 1920, provenance: 'generated' as const, inputHash: 'hash'};
const passingQualityInspector = {inspect: async () => ({report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/quality.json'})};
const createBullMqPipelineProcessor = (dependencies: Parameters<typeof createProductionProcessor>[0]) =>
  createProductionProcessor({...dependencies, qualityInspector: passingQualityInspector});
const avatar: Scene = {
  id: 'scene_001', order: 1, type: 'avatar', purpose: 'intro', script: 'Hello', durationEstimate: 2,
  visual: {layout: 'avatar_full', highlightWords: []}
};
const image: Scene = {
  id: 'scene_002', order: 2, type: 'image', purpose: 'visual', script: 'Product', durationEstimate: 2,
  visual: {layout: 'visual_full', highlightWords: []}
};
const factual: Scene = {
  id: 'scene_003', order: 3, type: 'screenshot', purpose: 'evidence', script: 'Show source', durationEstimate: 2,
  visual: {layout: 'visual_full', highlightWords: []},
  asset: {source: 'user-supplied', factual: true}
};

const projectRecord = (): ProjectRecord => ({
  id: 'project_001', title: 'fixture', status: 'CREATED',
  input: {type: 'script', content: 'fixture'},
  avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'},
  createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
});

const storyboard = (scenes: Scene[]): Storyboard => ({
  project: {title: 'fixture', width: 1080, height: 1920, fps: 30},
  scenes
});

const createQueue = (deliveries: BullMqSceneDelivery[]): QueueAdapter => ({
  submit: async (job: QueueJob) => {
    deliveries.push({
      name: job.name,
      data: {
        projectId: job.data.projectId as string,
        sceneId: job.data.sceneId as string,
        businessJobId: job.id,
        input: job.data.input
      },
      attemptsMade: 0
    });
    return {id: job.id, existing: false};
  }
});

describe('production pipeline worker composition', () => {
  it('makes a regeneration generation job claimable only after scene activation', async () => {
    const now = new Date('2026-01-01T00:00:00Z');
    const durable = new InMemoryProjectRepository(() => now);
    await durable.createProject({...projectRecord(), ownerUsername: 'alice', status: 'COMPLETED'});
    await durable.createScene({
      id: image.id, projectId: 'project_001', order: image.order, status: 'READY', script: image.script,
      visual: {...image.visual, sceneType: image.type, purpose: image.purpose, durationEstimate: image.durationEstimate, contentRevision: 0},
      createdAt: now.toISOString(), updatedAt: now.toISOString()
    });
    const deliveries: BullMqSceneDelivery[] = [];
    const queue = createQueue(deliveries);
    const dispatcher = new OutboxDispatcher(durable, queue, {owner: 'racing-dispatcher', now: () => now});
    const original = durable.reserveAndActivateSceneRegeneration.bind(durable);
    durable.reserveAndActivateSceneRegeneration = async (...args) => {
      const activated = await original(...args);
      expect(activated?.scene.visual.activeGenerationJobId).toBe(args[0].id);
      expect(await dispatcher.dispatchOnce()).toBe(1);
      return activated;
    };
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(durable, () => now),
      queue,
      buildStoryboard: () => storyboard([image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated}
    });
    const controlInput = {visual: {...image.visual, contentRevision: 0}};
    await durable.reserveJob({
      id: 'control-job', projectId: 'project_001', sceneId: image.id, taskType: 'scene.regenerate',
      inputHash: canonicalInputHash(controlInput), dispatchPayload: controlInput,
      status: 'QUEUED', createdAt: now.toISOString()
    });
    await processor({
      name: 'scene.regenerate',
      data: {projectId: 'project_001', sceneId: image.id, businessJobId: 'control-job', input: controlInput},
      attemptsMade: 0
    });
    expect(deliveries).toHaveLength(1);
    const generation = deliveries[0]!;
    expect((await durable.findJob(generation.data.businessJobId))?.status).toBe('QUEUED');
    expect((await durable.findProjectForWorker('project_001'))?.scenes[0]?.visual.activeGenerationJobId)
      .toBe(generation.data.businessJobId);
  });
  it('injects a publisher into production composition and streams committed worker mutations', async () => {
    const durable = new InMemoryProjectRepository(); const now = new Date().toISOString();
    await durable.createProject({id: 'project_factory', title: 'Factory', status: 'GENERATING_ASSETS', input: {type: 'script', content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'}, createdAt: now, updatedAt: now});
    await durable.createScene({id: 'scene_001', projectId: 'project_factory', order: 1, status: 'GENERATING', script: 'Scene', visual: {activeGenerationJobId: 'job_1'}, failureReason: 'old failure', createdAt: now, updatedAt: now});
    const broker = new InMemoryProjectEventBroker(); const frames: string[] = []; const initial = await durable.findProject('project_factory');
    openProjectEventStream({raw: {write: (frame: string) => frames.push(frame), once: () => undefined}, broker, project: initial!, heartbeatMs: 60_000});
    let captured: any;
    createProductionWorkerComposition({queueName: 'test', connection: {} as any, repository: durable, publisher: broker, buildStoryboard: () => ({project: {title: 'x', width: 1080, height: 1920, fps: 30}, scenes: []}), avatarProvider: {} as any, imageProvider: {} as any, qualityInspector: passingQualityInspector, queue: {submit: async () => ({id: 'x', existing: false})}, workerFactory: (options) => { captured = options; return {} as any; }});
    await captured.repository.markSceneFailedAndProjectPartiallyFailed('project_factory', 'scene_001', 'job_1', 'provider failed');
    expect((await durable.findProject('project_factory'))?.scenes[0].failureReason).toBe('provider failed');
    expect(frames.at(-1)).toContain('provider failed');
  });
  it('runs storyboard generation, scene workers, and durable fan-in through one queue flow', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    const alignedSnapshots: string[][] = [];
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([avatar, image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated},
      onProjectStatusPersisted: async ({status}) => {
        if (status === 'ALIGNING_TIMELINE') {
          alignedSnapshots.push((await task4.findProject(project.id))?.scenes.map((scene) => scene.status) ?? []);
        }
      }
    });

    await processor({
      name: 'storyboard.generate',
      data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'},
      attemptsMade: 0
    });

    expect(deliveries.map((delivery) => delivery.name).sort()).toEqual([
      'scene.asset.generate',
      'scene.avatar.generate'
    ]);
    expect((await task4.findProject(project.id))?.status).toBe('GENERATING_ASSETS');

    await processor(deliveries.shift()!);
    expect(alignedSnapshots).toEqual([]);
    expect((await task4.findProject(project.id))?.status).toBe('GENERATING_ASSETS');

    await processor(deliveries.shift()!);

    expect(alignedSnapshots).toEqual([['READY', 'READY']]);
    expect((await task4.findProject(project.id))?.status).toBe('COMPLETED');
  });

  it('persists a failed final quality gate from a project.render delivery', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = {...projectRecord(), status: 'QUALITY_CHECK' as const};
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    const processor = createProductionProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => ({
        report: {passed: false, errors: ['duration mismatch'], metrics: {}},
        reportPath: 'reports/project_001-quality.json'
      })}
    });

    await new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(0))
      .render((await task4.findProject(project.id))!);
    await processor(deliveries.shift()!);

    expect(await task4.findProject(project.id)).toMatchObject({
      status: 'FAILED', qualityReportPath: 'reports/project_001-quality.json'
    });
  });

  it('atomically marks provider exhaustion partial while preserving an independent ready version', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    let statusAtTerminalBoundary: string | undefined;
    class ObservingTerminalRepository extends Task4PipelineRepository {
      override async markSceneFailedAndProjectPartiallyFailed(projectId: string, sceneId: string, generationJobId: string) {
        statusAtTerminalBoundary = (await this.listScenes(projectId)).find((scene) => scene.id === sceneId)?.status;
        return super.markSceneFailedAndProjectPartiallyFailed(projectId, sceneId, generationJobId);
      }
    }
    const repository = new ObservingTerminalRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let providerCalls = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([avatar, image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        throw 'provider unavailable';
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    const avatarDelivery = deliveries.find((delivery) => delivery.name === 'scene.avatar.generate')!;
    const imageDelivery = deliveries.find((delivery) => delivery.name === 'scene.asset.generate')!;
    await processor(avatarDelivery);

    await expect(processor(imageDelivery)).rejects.toThrow('provider unavailable');
    await expect(processor({...imageDelivery, attemptsMade: 1})).rejects.toThrow('provider unavailable');
    await expect(processor({...imageDelivery, attemptsMade: 2})).rejects.toThrow(UnrecoverableError);

    const persisted = await task4.findProject(project.id);
    expect(providerCalls).toBe(3);
    expect(statusAtTerminalBoundary).toBe('GENERATING');
    expect(persisted?.status).toBe('PARTIALLY_FAILED');
    expect(persisted?.scenes.map((scene) => scene.status)).toEqual(['READY', 'FAILED']);
    expect(persisted?.assetVersions.map((asset) => asset.sceneId)).toEqual([avatar.id]);
  });

  it('routes API scene regeneration through a fresh durable generation job without touching ready scenes', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let avatarCalls = 0;
    let imageCalls = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([avatar, image]),
      avatarProvider: {generate: async () => {
        avatarCalls += 1;
        return {...generated, uri: 'mock://avatar'};
      }},
      imageProvider: {generate: async () => {
        imageCalls += 1;
        if (imageCalls <= 3) throw new Error('provider unavailable');
        return {...generated, uri: 'mock://recovered-image'};
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    const avatarDelivery = deliveries.find((delivery) => delivery.name === 'scene.avatar.generate')!;
    const exhaustedDelivery = deliveries.find((delivery) => delivery.name === 'scene.asset.generate')!;
    await processor(avatarDelivery);
    await expect(processor(exhaustedDelivery)).rejects.toThrow('provider unavailable');
    await expect(processor({...exhaustedDelivery, attemptsMade: 1})).rejects.toThrow('provider unavailable');
    await expect(processor({...exhaustedDelivery, attemptsMade: 2})).rejects.toThrow(UnrecoverableError);

    const failed = (await task4.findProject(project.id))!;
    const readyVersion = failed.assetVersions.find((asset) => asset.sceneId === avatar.id)!;
    deliveries.length = 0;
    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    const failedScene = failed.scenes.find((scene) => scene.id === image.id)!;
    await api.regenerateScene(failed, failedScene);

    expect(deliveries).toMatchObject([{name: 'scene.regenerate', data: {sceneId: image.id}}]);
    await processor(deliveries.shift()!);
    expect(deliveries).toMatchObject([{name: 'scene.asset.generate', data: {sceneId: image.id}}]);
    expect(deliveries[0]?.data.businessJobId).not.toBe(exhaustedDelivery.data.businessJobId);
    const retryGenerationJobId = deliveries[0]!.data.businessJobId;
    expect((await task4.findProject(project.id))?.status).toBe('GENERATING_ASSETS');

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    expect((await task4.findProject(project.id))?.scenes.find((scene) => scene.id === image.id)?.visual.activeGenerationJobId)
      .toBe(retryGenerationJobId);

    await expect(processor({...exhaustedDelivery, attemptsMade: 3})).rejects.toThrow(UnrecoverableError);
    expect((await task4.findProject(project.id))?.status).toBe('GENERATING_ASSETS');

    await processor(deliveries.shift()!);

    const recovered = await task4.findProject(project.id);
    expect(recovered?.status).toBe('COMPLETED');
    expect(avatarCalls).toBe(1);
    expect(imageCalls).toBe(4);
    expect(recovered?.assetVersions.filter((asset) => asset.sceneId === avatar.id)).toEqual([readyVersion]);
    expect(recovered?.assetVersions.filter((asset) => asset.sceneId === image.id)).toMatchObject([{uri: 'mock://recovered-image'}]);
  });

  it('regenerates one ready scene from a completed project and appends only its version', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let avatarCalls = 0;
    let imageCalls = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([avatar, image]),
      avatarProvider: {generate: async () => {
        avatarCalls += 1;
        return {...generated, uri: `mock://avatar/${avatarCalls}`};
      }},
      imageProvider: {generate: async () => {
        imageCalls += 1;
        return {...generated, uri: `mock://image/${imageCalls}`};
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    await Promise.all(deliveries.splice(0).map((delivery) => processor(delivery)));
    const completed = (await task4.findProject(project.id))!;
    const originalAvatarVersion = completed.assetVersions.find((asset) => asset.sceneId === avatar.id)!;
    const originalImageVersion = completed.assetVersions.find((asset) => asset.sceneId === image.id)!;
    expect(completed.status).toBe('COMPLETED');

    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    await api.regenerateScene(completed, completed.scenes.find((scene) => scene.id === image.id)!);
    await processor(deliveries.shift()!);

    const activated = await task4.findProject(project.id);
    expect(activated?.status).toBe('GENERATING_ASSETS');
    expect(activated?.scenes.map((scene) => scene.status)).toEqual(['READY', 'PENDING']);
    expect(activated?.assetVersions).toEqual([originalAvatarVersion, originalImageVersion]);

    await processor(deliveries.shift()!);

    const regenerated = await task4.findProject(project.id);
    expect(avatarCalls).toBe(1);
    expect(imageCalls).toBe(2);
    expect(regenerated?.status).toBe('COMPLETED');
    expect(regenerated?.assetVersions.filter((asset) => asset.sceneId === avatar.id)).toEqual([originalAvatarVersion]);
    expect(regenerated?.assetVersions.filter((asset) => asset.sceneId === image.id)).toMatchObject([
      originalImageVersion,
      {version: 2, uri: 'mock://image/2'}
    ]);
  });

  it('regenerates from the edited canonical narration and visual direction', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = {...projectRecord(), ownerUsername: 'alice'};
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    const providerPrompts: string[] = [];
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async (request) => {
        providerPrompts.push(request.prompt);
        return {...generated, uri: `mock://image/${providerPrompts.length}`};
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    await processor(deliveries.shift()!);
    const completed = (await task4.findProjectForWorker(project.id))!;
    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    await api.regenerateScene(completed, completed.scenes[0]!);
    await processor(deliveries.shift()!);
    const staleGeneration = deliveries.shift()!;
    expect((await task4.findJob(staleGeneration.data.businessJobId))?.options?.contentRevision).toBe(0);
    await api.patchScene(project.id, image.id, {
      script: 'Edited narration', visualPrompt: 'Edited visual direction'
    }, 'alice');
    expect((await repository.listScenes(project.id))[0]).toMatchObject({
      script: 'Edited narration', visualPrompt: 'Edited visual direction', contentRevision: 1
    });

    await processor(staleGeneration);
    expect(providerPrompts).toEqual(['Product']);

    const edited = (await task4.findProjectForWorker(project.id))!;
    await api.regenerateScene(edited, edited.scenes[0]!);
    await processor(deliveries.shift()!);
    const currentGeneration = deliveries.shift()!;
    expect(currentGeneration.data.businessJobId).not.toBe(staleGeneration.data.businessJobId);
    expect((await task4.findJob(currentGeneration.data.businessJobId))?.options?.contentRevision).toBe(1);
    await processor(currentGeneration);

    expect(providerPrompts).toEqual(['Product', 'Edited visual direction']);
    expect((await task4.findProjectForWorker(project.id))?.scenes[0]).toMatchObject({
      script: 'Edited narration', visual: {visualPrompt: 'Edited visual direction'}
    });
  });

  it('fences an in-flight provider result after an edit and regenerates only the edited revision', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = {...projectRecord(), ownerUsername: 'alice'};
    await task4.createProject(project);
    const now = new Date(0).toISOString();
    await task4.createScene({
      id: image.id, projectId: project.id, order: image.order, status: 'PENDING', script: 'Original narration',
      visual: {...image.visual, workerScene: {...image, script: 'Stale worker narration'}},
      createdAt: now, updatedAt: now
    });
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let releaseProvider!: () => void;
    const providerReleased = new Promise<void>((resolve) => { releaseProvider = resolve; });
    let markProviderStarted!: () => void;
    const providerStarted = new Promise<void>((resolve) => { markProviderStarted = resolve; });
    let calls = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async (request) => {
        calls += 1;
        if (calls === 1) {
          markProviderStarted();
          await providerReleased;
          return {...generated, uri: 'mock://stale', inputHash: request.prompt};
        }
        return {...generated, uri: 'mock://edited', inputHash: request.prompt};
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    const oldProcessing = processor(deliveries.shift()!);
    await providerStarted;

    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    await api.patchScene(project.id, image.id, {script: 'Edited narration', visualPrompt: 'Edited direction'}, 'alice');
    releaseProvider();
    await oldProcessing;

    const fenced = (await task4.findProjectForWorker(project.id))!;
    expect(fenced.scenes[0]).toMatchObject({status: 'GENERATING', script: 'Edited narration', visual: {contentRevision: 1}});
    expect(fenced.assetVersions).toEqual([]);

    await api.regenerateScene(fenced, fenced.scenes[0]!);
    await processor(deliveries.shift()!);
    await processor(deliveries.shift()!);

    expect((await task4.findProjectForWorker(project.id))?.assetVersions).toMatchObject([{uri: 'mock://edited', inputHash: 'Edited direction'}]);
  });

  it('keeps ready history when completed-project scene regeneration exhausts', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let failRegeneration = false;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([avatar, image]),
      avatarProvider: {generate: async () => ({...generated, uri: 'mock://avatar/original'})},
      imageProvider: {generate: async () => {
        if (failRegeneration) throw new Error('provider unavailable');
        return {...generated, uri: 'mock://image/original'};
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    await Promise.all(deliveries.splice(0).map((delivery) => processor(delivery)));
    const completed = (await task4.findProject(project.id))!;
    const originalVersions = completed.assetVersions;
    failRegeneration = true;

    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    await api.regenerateScene(completed, completed.scenes.find((scene) => scene.id === image.id)!);
    await processor(deliveries.shift()!);
    const generation = deliveries.shift()!;
    await expect(processor(generation)).rejects.toThrow('provider unavailable');
    await expect(processor({...generation, attemptsMade: 1})).rejects.toThrow('provider unavailable');
    await expect(processor({...generation, attemptsMade: 2})).rejects.toThrow(UnrecoverableError);

    const failed = await task4.findProject(project.id);
    expect(failed?.status).toBe('PARTIALLY_FAILED');
    expect(failed?.scenes.map((scene) => scene.status)).toEqual(['READY', 'FAILED']);
    expect(failed?.assetVersions).toEqual(originalVersions);
  });

  it('ignores a stale successful provider result after a newer regeneration becomes active', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let releaseOld!: () => void;
    const oldResult = new Promise<void>((resolve) => { releaseOld = resolve; });
    let markOldStarted!: () => void;
    const oldStarted = new Promise<void>((resolve) => { markOldStarted = resolve; });
    let calls = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        calls += 1;
        if (calls === 1) {
          markOldStarted();
          await oldResult;
          return {...generated, uri: 'mock://stale-image'};
        }
        return {...generated, uri: 'mock://current-image'};
      }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    const oldDelivery = deliveries.shift()!;
    const oldProcessing = processor(oldDelivery);
    await oldStarted;

    const generating = (await task4.findProject(project.id))!;
    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    await api.regenerateScene(generating, generating.scenes[0]!);
    await processor(deliveries.shift()!);
    const currentDelivery = deliveries.shift()!;

    releaseOld();
    await oldProcessing;

    const afterStaleResult = await task4.findProject(project.id);
    expect(afterStaleResult?.status).toBe('GENERATING_ASSETS');
    expect(afterStaleResult?.assetVersions).toEqual([]);
    expect(afterStaleResult?.scenes[0]?.visual.activeGenerationJobId).toBe(currentDelivery.data.businessJobId);

    await processor(currentDelivery);
    const completed = await task4.findProject(project.id);
    expect(completed?.status).toBe('COMPLETED');
    expect(completed?.assetVersions).toMatchObject([{uri: 'mock://current-image'}]);
  });

  it('does not let an older regeneration control replace a newer active attempt', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    deliveries.length = 0;
    const api = new ProjectService(task4, createQueue(deliveries), () => 'unused', () => new Date(1));
    const initial = (await task4.findProject(project.id))!;
    await api.regenerateScene(initial, initial.scenes[0]!);
    const olderControl = deliveries.shift()!;
    await processor(olderControl);
    const olderGeneration = deliveries.shift()!;

    const afterOlder = (await task4.findProject(project.id))!;
    await api.regenerateScene(afterOlder, afterOlder.scenes[0]!);
    await processor(deliveries.shift()!);
    const newerGeneration = deliveries.shift()!;
    expect(newerGeneration.data.businessJobId).not.toBe(olderGeneration.data.businessJobId);

    await processor(olderControl);
    expect((await task4.findProject(project.id))?.scenes[0]?.visual.activeGenerationJobId)
      .toBe(newerGeneration.data.businessJobId);
    expect(deliveries).toEqual([]);
  });

  it('keeps factual user input priority when another production scene exhausts', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([factual, image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => { throw new Error('provider unavailable'); }}
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    const generatedDelivery = deliveries[0]!;
    await expect(processor(generatedDelivery)).rejects.toThrow('provider unavailable');
    await expect(processor({...generatedDelivery, attemptsMade: 1})).rejects.toThrow('provider unavailable');
    await expect(processor({...generatedDelivery, attemptsMade: 2})).rejects.toThrow(UnrecoverableError);

    const persisted = await task4.findProject(project.id);
    expect(persisted?.status).toBe('NEEDS_USER_INPUT');
    expect(Object.fromEntries(persisted?.scenes.map((scene) => [scene.id, scene.status]) ?? [])).toEqual({
      [factual.id]: 'NEEDS_USER_INPUT',
      [image.id]: 'FAILED'
    });
  });

  it('atomically marks terminal quality exhaustion partial after two generations', async () => {
    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    let providerCalls = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        return generated;
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    const delivery = deliveries[0]!;
    await expect(processor(delivery)).rejects.toThrow('quality attempt 2');
    await expect(processor({...delivery, attemptsMade: 1})).rejects.toThrow(UnrecoverableError);

    const persisted = await task4.findProject(project.id);
    expect(providerCalls).toBe(2);
    expect(persisted?.status).toBe('PARTIALLY_FAILED');
    expect(persisted?.scenes[0]?.status).toBe('FAILED');
  });

  it('serializes concurrent final scene completions into one monotonic render path', async () => {
    let releaseReady!: () => void;
    const bothReady = new Promise<void>((resolve) => { releaseReady = resolve; });

    class InterleavingRepository extends Task4PipelineRepository {
      private readyWrites = 0;

      override async completeSceneGeneration(
        projectId: string,
        sceneId: string,
        generationJobId: string,
        contentRevision: number,
        asset: Parameters<Task4PipelineRepository['completeSceneGeneration']>[4]
      ): Promise<boolean> {
        const completed = await super.completeSceneGeneration(projectId, sceneId, generationJobId, contentRevision, asset);
        if (!completed) return false;
        this.readyWrites += 1;
        if (this.readyWrites === 2) releaseReady();
        await bothReady;
        return true;
      }
    }

    const task4 = new InMemoryProjectRepository();
    const project = projectRecord();
    await task4.createProject(project);
    const repository = new InterleavingRepository(task4, () => new Date(0));
    const deliveries: BullMqSceneDelivery[] = [];
    const persistedStatuses: ProjectStatus[] = [];
    const alignmentSnapshots: string[][] = [];
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: createQueue(deliveries),
      buildStoryboard: () => storyboard([avatar, image]),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated},
      onProjectStatusPersisted: async ({status}) => {
        persistedStatuses.push(status);
        if (status === 'ALIGNING_TIMELINE') {
          alignmentSnapshots.push((await task4.findProject(project.id))?.scenes.map((scene) => scene.status) ?? []);
        }
      }
    });

    await processor({name: 'storyboard.generate', data: {projectId: project.id, sceneId: 'project', businessJobId: 'storyboard-job'}, attemptsMade: 0});
    persistedStatuses.length = 0;
    await Promise.all(deliveries.map((delivery) => processor(delivery)));

    expect(persistedStatuses).toEqual(['ALIGNING_TIMELINE', 'RENDERING', 'QUALITY_CHECK', 'COMPLETED']);
    expect(alignmentSnapshots).toEqual([['READY', 'READY']]);
    expect((await task4.findProject(project.id))?.status).toBe('COMPLETED');
  });
});
