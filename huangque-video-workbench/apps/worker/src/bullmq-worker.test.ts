import {describe, expect, it} from 'vitest';
import {readFile} from 'node:fs/promises';
import {UnrecoverableError} from 'bullmq';
import type {Scene} from '@huangque/contracts';
import {InMemoryProjectRepository, canonicalInputHash, jobKey, type ProjectRecord} from '@huangque/api';
import {PipelineService, RenderLeaseLostError, Task4PipelineRepository, createSceneJobConsumer, type PipelineJob} from './pipeline.js';
import {createBullMqPipelineJobProcessor, createBullMqPipelineProcessor, createBullMqWorkerProcessor} from './bullmq-worker.js';

const generated = {uri: 'mock://asset', width: 1080, height: 1920, provenance: 'generated' as const, inputHash: 'hash'};
const scene: Scene = {
  id: 'scene_001', order: 1, type: 'image', purpose: 'visual', script: 'Product', durationEstimate: 2,
  visual: {layout: 'visual_full', highlightWords: []}
};

const seed = async (qualityAttempt = 1) => {
  const task4 = new InMemoryProjectRepository();
  const now = new Date(0).toISOString();
  const project: ProjectRecord = {
    id: 'project_001', title: 'fixture', status: 'GENERATING_ASSETS',
    input: {type: 'script', content: 'fixture'},
    avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'},
    createdAt: now, updatedAt: now
  };
  await task4.createProject(project);
  const repository = new Task4PipelineRepository(task4, () => new Date(0));
  await repository.persistScenes(project.id, [scene]);
  const name = 'scene.asset.generate';
  const id = jobKey(project.id, scene.id, name, canonicalInputHash({scene}));
  const job: PipelineJob = {
    id, name, data: {projectId: project.id, sceneId: scene.id}, status: 'QUEUED',
    options: {attempts: 3, backoff: {type: 'exponential', delay: 100}, qualityAttempt}
  };
  await repository.reserveJob(job);
  await repository.markJobQueued(id);
  await repository.claimSceneActiveGenerationJob(project.id, scene.id, undefined, id, 0);
  return {task4, repository, id};
};

const createRenderDelivery = async (task4: InMemoryProjectRepository, project: ProjectRecord) => {
  const input = {output: project.output, scenes: []};
  const id = jobKey(project.id, 'project', 'project.render', canonicalInputHash(input));
  await task4.reserveJob({
    id, projectId: project.id, sceneId: 'project', taskType: 'project.render',
    inputHash: canonicalInputHash(input), status: 'QUEUED', createdAt: new Date(0).toISOString()
  });
  return {name: 'project.render', data: {projectId: project.id, sceneId: 'project', businessJobId: id, input}, attemptsMade: 0};
};

describe('BullMQ worker bridge', () => {
  it('reloads durable job and quality state before every dispatch', async () => {
    const {repository, id} = await seed();
    await repository.updateJobGenerationState(id, {qualityAttempt: 2, deliveryAttemptsMade: 1});
    const deliveries: Array<{qualityAttempt: number; attemptsMade: number}> = [];
    const processor = createBullMqWorkerProcessor({
      repository,
      consumer: {consume: async ({job, attemptsMade}) => {
        deliveries.push({qualityAttempt: job.options.qualityAttempt, attemptsMade});
      }}
    });

    await processor({
      name: 'scene.asset.generate',
      data: {projectId: 'project_001', sceneId: 'scene_001', businessJobId: id},
      attemptsMade: 1
    });

    expect(deliveries).toEqual([{qualityAttempt: 2, attemptsMade: 1}]);
  });

  it('maps terminal quality failure to BullMQ UnrecoverableError and blocks a third generation', async () => {
    const {repository, id} = await seed(2);
    let providerCalls = 0;
    const consumer = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        return generated;
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });
    const processor = createBullMqWorkerProcessor({repository, consumer});
    const bullJob = {
      name: 'scene.asset.generate',
      data: {projectId: 'project_001', sceneId: 'scene_001', businessJobId: id},
      attemptsMade: 1
    };

    await expect(processor(bullJob)).rejects.toThrow(UnrecoverableError);
    await expect(processor({...bullJob, attemptsMade: 2})).rejects.toThrow(UnrecoverableError);

    expect(providerCalls).toBe(1);
  });

  it('rejects transport metadata that does not match the durable job', async () => {
    const {repository, id} = await seed();
    let consumerCalls = 0;
    const processor = createBullMqWorkerProcessor({
      repository,
      consumer: {consume: async () => { consumerCalls += 1; }}
    });

    await expect(processor({
      name: 'scene.avatar.generate',
      data: {projectId: 'wrong-project', sceneId: 'wrong-scene', businessJobId: id},
      attemptsMade: 0
    })).rejects.toThrow(UnrecoverableError);

    expect(consumerCalls).toBe(0);
  });

  it('dispatches project.render through the idempotent finalization path', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    await task4.createProject({
      id: 'project_render', title: 'render fixture', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'render fixture'},
      avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'},
      createdAt: now, updatedAt: now
    });
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    let inspections = 0;
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'render fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => {
        inspections += 1;
        return {report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/project_render-quality.json'};
      }}
    });
    const delivery = await createRenderDelivery(task4, (await task4.findProject('project_render'))!);

    await processor(delivery);
    await processor(delivery);

    expect(inspections).toBe(1);
    expect(await task4.findProject('project_render')).toMatchObject({
      status: 'COMPLETED', qualityReportPath: 'reports/project_render-quality.json'
    });
  });

  it('preserves failed and cancelled render terminals without reopening them', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const failed: ProjectRecord = {
      id: 'project_failed_render', title: 'failed render', status: 'FAILED', qualityReportPath: 'reports/failed.json',
      input: {type: 'script', content: 'failed'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    const cancelled: ProjectRecord = {...failed, id: 'project_cancelled_render', status: 'CANCELLED', qualityReportPath: undefined};
    await task4.createProject(failed);
    await task4.createProject(cancelled);
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)),
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { throw new Error('terminal project was reopened'); }}
    });

    await processor(await createRenderDelivery(task4, failed));
    await processor(await createRenderDelivery(task4, cancelled));

    expect(await task4.findProject(failed.id)).toMatchObject({status: 'FAILED', qualityReportPath: 'reports/failed.json'});
    expect(await task4.findProject(cancelled.id)).toMatchObject({status: 'CANCELLED'});
  });

  it('persists inspector exceptions as terminal failed results and bounds BullMQ retries', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_broken_render', title: 'broken render', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'broken'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    let inspections = 0;
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)),
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { inspections += 1; throw new Error('renderer crashed'); }}
    });
    const delivery = await createRenderDelivery(task4, project);

    await expect(processor(delivery)).rejects.toThrow(UnrecoverableError);
    await processor(delivery);

    expect(inspections).toBe(1);
    const persisted = await task4.findProject(project.id);
    expect(persisted).toMatchObject({status: 'FAILED', qualityReportPath: expect.stringContaining(project.id)});
    expect(JSON.parse(await readFile(persisted!.qualityReportPath!, 'utf8'))).toMatchObject({passed: false, errors: ['renderer crashed']});
  });

  it.each([
    ['times out', async () => await new Promise<string>(() => undefined)],
    ['throws', async () => { throw new Error('report filesystem unavailable'); }]
  ])('persists terminal failure when the unique failure report writer %s', async (_case, writer) => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_report_timeout', title: 'report timeout', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'report timeout'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)), queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { throw new Error('renderer crashed'); }},
      renderFailureReportDeadlineMs: 10,
      renderFailureReportWriter: writer
    } as any);
    const delivery = await createRenderDelivery(task4, project);

    await expect(processor(delivery)).rejects.toThrow(UnrecoverableError);
    expect(await task4.findProject(project.id)).toMatchObject({status: 'FAILED', qualityReportPath: expect.stringContaining('report-write-error')});
    expect((await task4.findJob(delivery.data.businessJobId))?.status).toBe('FAILED');
  });

  it('bounds a never-settling render inspector and prevents a duplicate side effect', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_hung_render', title: 'hung render', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'hung'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    let inspections = 0;
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)),
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { inspections += 1; return await new Promise(() => undefined); }},
      renderDeadlineMs: 20
    } as any);
    const delivery = await createRenderDelivery(task4, project);

    await expect(processor(delivery)).rejects.toThrow(UnrecoverableError);
    await processor(delivery);

    expect(inspections).toBe(1);
    expect((await task4.findProject(project.id))?.status).toBe('FAILED');
  }, 1_000);

  it('serializes concurrent project.render deliveries into one inspector invocation', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_concurrent_render', title: 'concurrent render', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'concurrent'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    let releaseInspection!: () => void;
    const inspectionGate = new Promise<void>((resolve) => { releaseInspection = resolve; });
    let inspectorStarted!: () => void;
    const inspectorHasStarted = new Promise<void>((resolve) => { inspectorStarted = resolve; });
    let inspections = 0;
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)),
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => {
        inspections += 1;
        inspectorStarted();
        await inspectionGate;
        return {report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/project_concurrent_render-quality.json'};
      }}
    });
    const delivery = await createRenderDelivery(task4, project);

    const first = processor(delivery);
    await inspectorHasStarted;
    const second = processor(delivery);
    releaseInspection();
    await Promise.all([first, second]);

    expect(inspections).toBe(1);
    expect((await task4.findProject(project.id))?.status).toBe('COMPLETED');
  });

  it('uses a durable render lease across two pipeline services sharing one repository', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_distributed_render', title: 'distributed render', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'distributed'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    let releaseInspection!: () => void;
    const gate = new Promise<void>((resolve) => { releaseInspection = resolve; });
    let firstStarted!: () => void;
    const firstInspection = new Promise<void>((resolve) => { firstStarted = resolve; });
    let secondStarted!: () => void;
    const secondInspection = new Promise<void>((resolve) => { secondStarted = resolve; });
    let inspections = 0;
    const dependencies = {
      queue: {submit: async (job: {id: string}) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => {
        inspections += 1;
        if (inspections === 1) firstStarted();
        if (inspections === 2) secondStarted();
        await gate;
        return {report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/project_distributed_render-quality.json'};
      }}
    };
    const firstProcessor = createBullMqPipelineProcessor({repository: new Task4PipelineRepository(task4, () => new Date(0)), ...dependencies});
    const secondProcessor = createBullMqPipelineProcessor({repository: new Task4PipelineRepository(task4, () => new Date(0)), ...dependencies});
    const delivery = await createRenderDelivery(task4, project);

    const first = firstProcessor(delivery);
    await firstInspection;
    const second = secondProcessor(delivery);
    releaseInspection();
    await Promise.all([first, second]);

    expect(inspections).toBe(1);
  });

  it('fences a late expired owner so only the takeover attempt publishes output', async () => {
    let clockMs = 0;
    const now = () => new Date(clockMs);
    const task4 = new InMemoryProjectRepository(now);
    const timestamp = now().toISOString();
    const project: ProjectRecord = {
      id: 'project_fenced_takeover', title: 'fenced takeover', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'fenced'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'},
      createdAt: timestamp, updatedAt: timestamp
    };
    await task4.createProject(project);
    const delivery = await createRenderDelivery(task4, project);
    let releaseLateAttempt!: () => void;
    const lateAttempt = new Promise<void>((resolve) => { releaseLateAttempt = resolve; });
    let losingAttemptStarted!: () => void;
    const losingAttemptStart = new Promise<void>((resolve) => { losingAttemptStarted = resolve; });
    let losingAttemptLostLease!: () => void;
    const losingAttemptLeaseLoss = new Promise<void>((resolve) => { losingAttemptLostLease = resolve; });
    let inspections = 0;
    const published: string[] = [];
    const qualityInspector = {inspect: async (_project: unknown, signal?: AbortSignal, attempt?: {owner: string}) => {
      inspections += 1;
      if (inspections === 1) {
        clockMs = 100;
        signal?.addEventListener('abort', () => losingAttemptLostLease(), {once: true});
        losingAttemptStarted();
        await lateAttempt;
        return {report: {passed: true, errors: [], metrics: {}}, reportPath: `reports/${attempt!.owner}/late.json`, previewUrl: `attempts/${attempt!.owner}/late.mp4`, publish: () => published.push(attempt!.owner)};
      }
      return {report: {passed: true, errors: [], metrics: {}}, reportPath: `reports/${attempt!.owner}/winner.json`, previewUrl: `attempts/${attempt!.owner}/winner.mp4`, downloadUrl: `attempts/${attempt!.owner}/winner.mp4`, publish: () => published.push(attempt!.owner)};
    }};
    const dependencies = {
      queue: {submit: async (job: {id: string}) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated}, qualityInspector,
      now, renderLeaseMs: 50, renderLeaseHeartbeatMs: 5
    };
    const first = new PipelineService({repository: new Task4PipelineRepository(task4, now), ...dependencies})
      .processRenderJob(project.id, delivery.data.businessJobId, new AbortController().signal);
    await losingAttemptStart;
    await losingAttemptLeaseLoss;
    await new PipelineService({repository: new Task4PipelineRepository(task4, now), ...dependencies})
      .processRenderJob(project.id, delivery.data.businessJobId, new AbortController().signal);
    releaseLateAttempt();
    await expect(first).rejects.toBeInstanceOf(RenderLeaseLostError);

    const persisted = await task4.findProject(project.id);
    expect(inspections).toBe(2);
    expect(persisted).toMatchObject({status: 'COMPLETED', qualityReportPath: expect.stringContaining('winner.json'), previewUrl: expect.stringContaining('winner.mp4')});
    expect(persisted?.previewUrl).not.toContain('late.mp4');
    expect(published).toHaveLength(1);
  });

  it('rejects a non-cooperative inspector as soon as renewal loses the lease', async () => {
    let clockMs = 0;
    const now = () => new Date(clockMs);
    const task4 = new InMemoryProjectRepository(now);
    const project: ProjectRecord = {
      id: 'project_lease_abort', title: 'lease abort', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'lease abort'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'},
      createdAt: now().toISOString(), updatedAt: now().toISOString()
    };
    await task4.createProject(project);
    const delivery = await createRenderDelivery(task4, project);
    let started!: () => void;
    const inspectionStarted = new Promise<void>((resolve) => { started = resolve; });
    const service = new PipelineService({
      repository: new Task4PipelineRepository(task4, now), queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { clockMs = 100; started(); return await new Promise(() => undefined); }},
      now, renderLeaseMs: 50, renderLeaseHeartbeatMs: 5
    });
    const processing = service.processRenderJob(project.id, delivery.data.businessJobId, new AbortController().signal);
    void processing.catch(() => undefined);
    await inspectionStarted;
    const outcome = await Promise.race([
      processing.then(() => new Error('render unexpectedly fulfilled'), (error) => error),
      new Promise<Error>((resolve) => setTimeout(() => resolve(new Error('lease-loss propagation timed out')), 100))
    ]);
    expect(outcome).toBeInstanceOf(RenderLeaseLostError);
  });

  it('serializes delayed heartbeat renewal and converts its rejection into lease loss', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_renewal_error', title: 'renewal error', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'renewal error'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    const delivery = await createRenderDelivery(task4, project);
    let releaseRenewal!: () => void;
    const renewalGate = new Promise<void>((resolve) => { releaseRenewal = resolve; });
    let renewalCalls = 0;
    class RejectingRenewalRepository extends Task4PipelineRepository {
      override async renewRenderJobLease(): Promise<undefined> {
        renewalCalls += 1;
        await renewalGate;
        throw new Error('lease store unavailable');
      }
    }
    let started!: () => void;
    const inspectionStarted = new Promise<void>((resolve) => { started = resolve; });
    const service = new PipelineService({
      repository: new RejectingRenewalRepository(task4, () => new Date(0)), queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { started(); return await new Promise(() => undefined); }},
      renderLeaseMs: 50, renderLeaseHeartbeatMs: 5
    });
    const processing = service.processRenderJob(project.id, delivery.data.businessJobId, new AbortController().signal);
    void processing.catch(() => undefined);
    await inspectionStarted;
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(renewalCalls).toBe(1);
    releaseRenewal();
    await expect(processing).rejects.toBeInstanceOf(RenderLeaseLostError);
    expect(await task4.findProject(project.id)).toMatchObject({status: 'FAILED', qualityReportPath: expect.stringContaining('render-lease-lost')});
    expect((await task4.findJob(delivery.data.businessJobId))?.status).toBe('FAILED');
  });

  it('forwards BullMQ cancellation through the arity-three production callback', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_cancelled_delivery', title: 'cancelled delivery', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'cancelled delivery'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    let inspections = 0;
    const processor = createBullMqPipelineJobProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)),
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { inspections += 1; return {report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/quality.json'}; }}
    });
    const controller = new AbortController();
    controller.abort(new Error('worker shutdown'));

    await expect(processor(await createRenderDelivery(task4, project), undefined, controller.signal)).rejects.toThrow('worker shutdown');

    expect(processor.length).toBeGreaterThanOrEqual(3);
    expect(inspections).toBe(0);
    expect((await task4.findProject(project.id))?.status).toBe('CANCELLED');
    expect((await task4.findJob((await createRenderDelivery(task4, project)).data.businessJobId))?.status).toBe('CANCELLED');
  });

  it('preserves an already terminal project before honoring a pre-aborted render delivery', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_terminal_preabort', title: 'terminal preabort', status: 'COMPLETED', qualityReportPath: 'reports/winner.json', previewUrl: 'attempts/winner/preview.mp4',
      input: {type: 'script', content: 'terminal'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)), queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { throw new Error('terminal output must not be inspected'); }}
    });
    const controller = new AbortController();
    controller.abort(new Error('worker shutdown'));
    const delivery = await createRenderDelivery(task4, project);

    await expect(processor({...delivery, signal: controller.signal})).resolves.toBeUndefined();
    expect(await task4.findProject(project.id)).toMatchObject({status: 'COMPLETED', qualityReportPath: 'reports/winner.json', previewUrl: 'attempts/winner/preview.mp4'});
    expect((await task4.findJob(delivery.data.businessJobId))?.status).toBe('COMPLETED');
  });

  it('rejects project.render deliveries whose durable metadata or input does not match', async () => {
    const task4 = new InMemoryProjectRepository();
    const now = new Date(0).toISOString();
    const project: ProjectRecord = {
      id: 'project_metadata_render', title: 'metadata render', status: 'QUALITY_CHECK',
      input: {type: 'script', content: 'metadata'}, avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now
    };
    await task4.createProject(project);
    let inspections = 0;
    const processor = createBullMqPipelineProcessor({
      repository: new Task4PipelineRepository(task4, () => new Date(0)),
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated}, imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => { inspections += 1; return {report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/quality.json'}; }}
    });
    const delivery = await createRenderDelivery(task4, project);

    await expect(processor({...delivery, data: {...delivery.data, input: {output: {templateId: 'tampered'}, scenes: []}}})).rejects.toThrow(UnrecoverableError);
    await expect(processor({...delivery, data: {...delivery.data, projectId: 'wrong-project'}})).rejects.toThrow(UnrecoverableError);

    expect(inspections).toBe(0);
    expect((await task4.findProject(project.id))?.status).toBe('QUALITY_CHECK');
  });

  it('keeps unknown production job names unrecoverable', async () => {
    const task4 = new InMemoryProjectRepository();
    const repository = new Task4PipelineRepository(task4, () => new Date(0));
    const processor = createBullMqPipelineProcessor({
      repository,
      queue: {submit: async (job) => ({id: job.id, existing: false})},
      buildStoryboard: () => ({project: {title: 'fixture', width: 1080, height: 1920, fps: 30}, scenes: []}),
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => generated},
      qualityInspector: {inspect: async () => ({report: {passed: true, errors: [], metrics: {}}, reportPath: 'reports/quality.json'})}
    });

    await expect(processor({
      name: 'project.unknown',
      data: {projectId: 'project_render', sceneId: 'project', businessJobId: 'unknown-job'},
      attemptsMade: 0
    })).rejects.toThrow(UnrecoverableError);
  });
});
