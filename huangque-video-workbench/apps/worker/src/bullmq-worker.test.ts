import {describe, expect, it} from 'vitest';
import {UnrecoverableError} from 'bullmq';
import type {Scene} from '@huangque/contracts';
import {InMemoryProjectRepository, canonicalInputHash, jobKey, type ProjectRecord} from '@huangque/api';
import {Task4PipelineRepository, createSceneJobConsumer, type PipelineJob} from './pipeline.js';
import {createBullMqWorkerProcessor} from './bullmq-worker.js';

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
  await repository.claimSceneActiveGenerationJob(project.id, scene.id, undefined, id);
  return {task4, repository, id};
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
});
