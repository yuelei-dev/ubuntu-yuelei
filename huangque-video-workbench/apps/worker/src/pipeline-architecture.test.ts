import {describe, expect, it, vi} from 'vitest';
import type {Scene, Storyboard} from '@huangque/contracts';
import {InMemoryProjectRepository, type ProjectRecord} from '@huangque/api';
import type {AvatarProvider, ImageProvider} from '@huangque/providers';
import {
  PipelineService,
  Task4PipelineRepository,
  createOfflineBullMqJobRunner,
  createSceneJobConsumer,
  type PipelineDependencies
} from './pipeline.js';
import {allRequiredScenesReady} from './processors/render.js';

const generated = {uri: 'mock://asset', width: 1080, height: 1920, provenance: 'generated' as const, inputHash: 'hash'};
const passingQualityInspector = {inspect: async () => ({report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/quality.json'})};

const avatarScene = (id = 'scene_001'): Scene => ({
  id, order: 1, type: 'avatar', purpose: 'intro', script: 'Hello', durationEstimate: 2,
  visual: {layout: 'avatar_full', highlightWords: []}
});

const imageScene = (id = 'scene_002'): Scene => ({
  id, order: 2, type: 'image', purpose: 'visual', script: 'Product', durationEstimate: 2,
  visual: {layout: 'visual_full', highlightWords: []}
});

const board = (scenes: Scene[]): Storyboard => ({
  project: {title: 'fixture', width: 1080, height: 1920, fps: 30},
  scenes
});

const projectRecord = (status: ProjectRecord['status'] = 'CREATED'): ProjectRecord => {
  const now = new Date(0).toISOString();
  return {
    id: 'project_001', title: 'fixture', status,
    input: {type: 'script', content: 'fixture'},
    avatar: {avatarId: 'mock', voiceId: 'mock'},
    output: {templateId: 'vertical_knowledge_v1'},
    createdAt: now, updatedAt: now
  };
};

const createService = (input: {
  task4: InMemoryProjectRepository;
  repository?: Task4PipelineRepository;
  scenes: Scene[];
  avatarProvider?: AvatarProvider;
  imageProvider?: ImageProvider;
  assessGeneratedMedia?: (input: Parameters<NonNullable<PipelineDependencies['assessGeneratedMedia']>>[0]) => {accepted: boolean; reason?: string};
  onRetry?: NonNullable<PipelineDependencies['onRetry']>;
  onProjectStatusPersisted?: NonNullable<PipelineDependencies['onProjectStatusPersisted']>;
  submitted?: string[];
}) => {
  const repository = input.repository ?? new Task4PipelineRepository(input.task4, () => new Date(0));
  const avatarProvider = input.avatarProvider ?? {generate: async () => generated};
  const imageProvider = input.imageProvider ?? {generate: async () => generated};
  const consumer = createSceneJobConsumer({
    repository,
    avatarProvider,
    imageProvider,
    assessGeneratedMedia: input.assessGeneratedMedia,
    onRetry: input.onRetry
  });
  const jobRunner = createOfflineBullMqJobRunner(repository, consumer);
  const service = new PipelineService({
    repository,
    queue: {submit: async (job) => {
      input.submitted?.push(job.id);
      return {id: job.id, existing: false};
    }},
    buildStoryboard: () => board(input.scenes),
    avatarProvider,
    imageProvider,
    qualityInspector: passingQualityInspector,
    jobRunner,
    onProjectStatusPersisted: input.onProjectStatusPersisted
  });
  return {repository, service};
};

describe('pipeline architecture', () => {
  it('models three BullMQ deliveries for the three-call transient cap', async () => {
    const task4 = new InMemoryProjectRepository();
    await task4.createProject(projectRecord());
    let providerCalls = 0;
    const backoffs: number[] = [];
    const {service} = createService({
      task4,
      scenes: [imageScene()],
      imageProvider: {generate: async () => {
        providerCalls += 1;
        throw new Error('network unavailable');
      }},
      onRetry: ({delayMs}) => backoffs.push(delayMs)
    });

    await expect(service.runProjectPipeline('project_001')).rejects.toThrow('network unavailable');

    expect(providerCalls).toBe(3);
    expect(backoffs).toEqual([100, 200]);
    expect((await task4.findProject('project_001'))?.jobs[0]?.options).toEqual({
      attempts: 3,
      backoff: {type: 'exponential', delay: 100},
      qualityAttempt: 1,
      deliveryAttemptsMade: 3
    });
  });

  it('persists a qualityAttempt capped at two total generated assets', async () => {
    const task4 = new InMemoryProjectRepository();
    await task4.createProject(projectRecord());
    let providerCalls = 0;
    const {service} = createService({
      task4,
      scenes: [imageScene()],
      imageProvider: {generate: async () => {
        providerCalls += 1;
        return {...generated, uri: `mock://asset/${providerCalls}`};
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });

    await expect(service.runProjectPipeline('project_001')).rejects.toThrow('blurred');

    expect(providerCalls).toBe(2);
    expect((await task4.findProject('project_001'))?.jobs[0]?.options?.qualityAttempt).toBe(2);
  });

  it('emits persisted ALIGNING_TIMELINE only after every scene is READY or fallback-accepted', async () => {
    const task4 = new InMemoryProjectRepository();
    await task4.createProject(projectRecord());
    const fallback = imageScene();
    const now = new Date(0).toISOString();
    await task4.createScene({
      id: fallback.id,
      projectId: 'project_001',
      order: fallback.order,
      status: 'FALLBACK_ACCEPTED',
      script: fallback.script,
      visual: {...fallback.visual, workerScene: fallback},
      asset: null,
      createdAt: now,
      updatedAt: now
    });
    let releaseAvatar!: () => void;
    const avatarGate = new Promise<void>((resolve) => { releaseAvatar = resolve; });
    const alignedSnapshots: string[][] = [];
    const {service} = createService({
      task4,
      scenes: [avatarScene(), fallback],
      avatarProvider: {generate: async () => {
        await avatarGate;
        return generated;
      }},
      onProjectStatusPersisted: async ({status}) => {
        if (status === 'ALIGNING_TIMELINE') {
          alignedSnapshots.push((await task4.findProject('project_001'))?.scenes.map((scene) => scene.status) ?? []);
        }
      }
    });

    const running = service.runProjectPipeline('project_001');
    await vi.waitFor(async () => {
      expect((await task4.findProject('project_001'))?.scenes.find((scene) => scene.id === 'scene_001')?.status).toBe('GENERATING');
    });
    expect(alignedSnapshots).toEqual([]);

    releaseAvatar();
    await running;

    expect(alignedSnapshots).toEqual([['READY', 'FALLBACK_ACCEPTED']]);
  });

  it('resumes durable jobs and assets across two fresh worker services sharing Task 4 state', async () => {
    const task4 = new InMemoryProjectRepository();
    await task4.createProject(projectRecord());
    class CrashAfterQualityPersistenceRepository extends Task4PipelineRepository {
      override async claimJobQuality(
        id: string,
        expectedQualityAttempt: number,
        patch: {qualityAttempt?: number; qualityTerminal?: boolean}
      ) {
        const claimed = await super.claimJobQuality(id, expectedQualityAttempt, patch);
        if (patch.qualityAttempt === 2) throw new Error('worker one crashed after persisting quality state');
        return claimed;
      }
    }
    const firstSubmissions: string[] = [];
    let firstWorkerCalls = 0;
    const firstWorker = createService({
      task4,
      repository: new CrashAfterQualityPersistenceRepository(task4, () => new Date(0)),
      scenes: [imageScene()],
      submitted: firstSubmissions,
      imageProvider: {generate: async () => {
        firstWorkerCalls += 1;
        return generated;
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });
    await expect(firstWorker.service.runProjectPipeline('project_001'))
      .rejects.toThrow('worker one crashed after persisting quality state');

    const secondSubmissions: string[] = [];
    let secondWorkerCalls = 0;
    const secondWorker = createService({
      task4,
      scenes: [imageScene()],
      submitted: secondSubmissions,
      imageProvider: {generate: async () => {
        secondWorkerCalls += 1;
        return generated;
      }}
    });
    await secondWorker.service.runProjectPipeline('project_001');

    const persisted = await task4.findProject('project_001');
    expect(firstWorkerCalls).toBe(1);
    expect(secondWorkerCalls).toBe(1);
    expect(firstSubmissions).toHaveLength(1);
    expect(secondSubmissions).toHaveLength(0);
    expect(persisted?.jobs[0]?.options).toMatchObject({attempts: 3, qualityAttempt: 2});
    expect(persisted?.scenes[0]?.status).toBe('READY');
    expect(persisted?.assetVersions).toHaveLength(1);
    expect(persisted?.status).toBe('COMPLETED');
  });

  it('rejects an empty required-scene set at fan-in', () => {
    expect(allRequiredScenesReady([])).toBe(false);
  });
});
