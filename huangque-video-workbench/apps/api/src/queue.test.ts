import {describe, expect, it} from 'vitest';
import {BullMqQueueAdapter, bullMqJobId} from './queue.js';

describe('BullMqQueueAdapter', () => {
  it('forwards retry settings but keeps durable quality state out of BullMQ options', async () => {
    let receivedOptions: unknown;
    let receivedData: Record<string, unknown> | undefined;
    const adapter = new BullMqQueueAdapter({
      add: async (_name, data, options) => {
        receivedData = data;
        receivedOptions = options;
        return {id: options.jobId};
      }
    });
    const businessKey = 'project_001:scene_001:scene.asset.generate:hash';

    await adapter.submit({
      id: businessKey,
      name: 'scene.asset.generate',
      data: {projectId: 'project_001', sceneId: 'scene_001'},
      options: {
        attempts: 3,
        backoff: {type: 'exponential', delay: 100},
        qualityAttempt: 2
      }
    });

    expect(receivedOptions).toEqual({
      jobId: bullMqJobId(businessKey),
      attempts: 3,
      backoff: {type: 'exponential', delay: 100}
    });
    expect(receivedData).toEqual({
      projectId: 'project_001',
      sceneId: 'scene_001',
      businessJobId: businessKey
    });
  });
});
