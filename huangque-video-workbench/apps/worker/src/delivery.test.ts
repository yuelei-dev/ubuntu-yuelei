import {describe, expect, it} from 'vitest';
import type {Scene} from '@huangque/contracts';
import {InMemoryProjectRepository, jobKey, canonicalInputHash, type ProjectRecord} from '@huangque/api';
import {
  DeliveryAttemptsExhaustedError,
  GeneratedMediaQualityError,
  QualityRetryRequestedError,
  Task4PipelineRepository,
  createSceneJobConsumer,
  type PipelineJob
} from './pipeline.js';

const generated = {uri: 'mock://asset', width: 1080, height: 1920, provenance: 'generated' as const, inputHash: 'hash'};
const scene: Scene = {
  id: 'scene_001', order: 1, type: 'image', purpose: 'visual', script: 'Product', durationEstimate: 2,
  visual: {layout: 'visual_full', highlightWords: []}
};

const seed = async () => {
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
    options: {attempts: 3, backoff: {type: 'exponential', delay: 100}, qualityAttempt: 1}
  };
  await repository.reserveJob(job);
  await repository.markJobQueued(id);
  await repository.claimSceneActiveGenerationJob(project.id, scene.id, undefined, id);
  return {task4, repository, id};
};

describe('scene job deliveries', () => {
  it('performs one provider call for one BullMQ delivery', async () => {
    const {repository, id} = await seed();
    let providerCalls = 0;
    const consumer = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        throw new Error('network unavailable');
      }}
    });
    const job = await repository.findJob(id);

    await expect(consumer.consume({job, attemptsMade: 0})).rejects.toThrow('network unavailable');

    expect(providerCalls).toBe(1);
    expect((await repository.findJob(id)).options.deliveryAttemptsMade).toBe(1);
  });

  it('uses three separate deliveries for the three-call transient cap', async () => {
    const {repository, id} = await seed();
    let providerCalls = 0;
    const delays: number[] = [];
    const consumer = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        throw new Error('network unavailable');
      }},
      onRetry: ({delayMs}) => delays.push(delayMs)
    });

    for (let attemptsMade = 0; attemptsMade < 3; attemptsMade += 1) {
      const job = await repository.findJob(id);
      await expect(consumer.consume({job, attemptsMade})).rejects.toThrow('network unavailable');
    }

    expect(providerCalls).toBe(3);
    expect(delays).toEqual([100, 200]);
    expect((await repository.findJob(id)).options.deliveryAttemptsMade).toBe(3);
  });

  it('does not reset durable delivery accounting across new consumers', async () => {
    const {repository, id} = await seed();
    let providerCalls = 0;
    for (let restart = 0; restart < 3; restart += 1) {
      const consumer = createSceneJobConsumer({
        repository,
        avatarProvider: {generate: async () => generated},
        imageProvider: {generate: async () => {
          providerCalls += 1;
          throw new Error('network unavailable');
        }}
      });
      await expect(consumer.consume({job: await repository.findJob(id), attemptsMade: 0}))
        .rejects.toThrow('network unavailable');
    }

    const restarted = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        throw new Error('must not be called');
      }}
    });
    await expect(restarted.consume({job: await repository.findJob(id), attemptsMade: 0}))
      .rejects.toThrow(DeliveryAttemptsExhaustedError);
    expect(providerCalls).toBe(3);
  });

  it('persists terminal quality state and never generates a third asset', async () => {
    const {repository, id} = await seed();
    let providerCalls = 0;
    const consumer = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        return {...generated, uri: `mock://asset/${providerCalls}`};
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });

    await expect(consumer.consume({job: await repository.findJob(id), attemptsMade: 0}))
      .rejects.toThrow(QualityRetryRequestedError);
    await expect(consumer.consume({job: await repository.findJob(id), attemptsMade: 1}))
      .rejects.toThrow(GeneratedMediaQualityError);
    await expect(consumer.consume({job: await repository.findJob(id), attemptsMade: 2}))
      .rejects.toThrow(GeneratedMediaQualityError);

    expect(providerCalls).toBe(2);
    expect((await repository.findJob(id)).options).toMatchObject({qualityAttempt: 2, qualityTerminal: true});
  });

  it('atomically allows only one overlapping delivery to invoke the provider', async () => {
    const {repository, id} = await seed();
    let providerCalls = 0;
    const consumer = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        await Promise.resolve();
        return generated;
      }}
    });
    const staleJob = await repository.findJob(id);

    const results = await Promise.allSettled([
      consumer.consume({job: staleJob, attemptsMade: 0}),
      consumer.consume({job: staleJob, attemptsMade: 0})
    ]);

    expect(providerCalls).toBe(1);
    expect(results.filter((result) => result.status === 'fulfilled')).toHaveLength(1);
    expect((await repository.findJob(id)).options.deliveryAttemptsMade).toBe(1);
  });

  it('atomically caps quality generation under overlapping deliveries', async () => {
    const {repository, id} = await seed();
    let providerCalls = 0;
    const consumer = createSceneJobConsumer({
      repository,
      avatarProvider: {generate: async () => generated},
      imageProvider: {generate: async () => {
        providerCalls += 1;
        await Promise.resolve();
        return generated;
      }},
      assessGeneratedMedia: () => ({accepted: false, reason: 'blurred'})
    });

    const firstQualityJob = await repository.findJob(id);
    await Promise.allSettled([
      consumer.consume({job: firstQualityJob, attemptsMade: 0}),
      consumer.consume({job: firstQualityJob, attemptsMade: 0})
    ]);
    const secondQualityJob = await repository.findJob(id);
    await Promise.allSettled([
      consumer.consume({job: secondQualityJob, attemptsMade: 1}),
      consumer.consume({job: secondQualityJob, attemptsMade: 1})
    ]);

    expect(providerCalls).toBe(2);
    expect((await repository.findJob(id)).options).toMatchObject({
      deliveryAttemptsMade: 2,
      qualityAttempt: 2,
      qualityTerminal: true
    });
  });
});
