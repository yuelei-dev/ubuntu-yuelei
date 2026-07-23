import {describe, expect, it, vi} from 'vitest';
import {JobNameProcessorRegistry, UnknownJobNameError, type SceneJob} from './registry.js';

const job = (name: string): SceneJob => ({
  id: `project_001:scene_001:${name}:hash`,
  name,
  data: {projectId: 'project_001', sceneId: 'scene_001'},
  options: {attempts: 3, backoff: {type: 'exponential', delay: 100}, qualityAttempt: 1},
  status: 'QUEUED'
});

describe('JobNameProcessorRegistry', () => {
  it('dispatches asset and avatar jobs only to their registered processors', async () => {
    const asset = {process: vi.fn(async () => undefined)};
    const avatar = {process: vi.fn(async () => undefined)};
    const registry = new JobNameProcessorRegistry({
      'scene.asset.generate': asset,
      'scene.avatar.generate': avatar
    });

    await registry.dispatch(job('scene.asset.generate'));
    await registry.dispatch(job('scene.avatar.generate'));

    expect(asset.process).toHaveBeenCalledOnce();
    expect(asset.process).toHaveBeenCalledWith(expect.objectContaining({name: 'scene.asset.generate'}));
    expect(avatar.process).toHaveBeenCalledOnce();
    expect(avatar.process).toHaveBeenCalledWith(expect.objectContaining({name: 'scene.avatar.generate'}));
  });

  it('fails explicitly for an unknown job name', async () => {
    const registry = new JobNameProcessorRegistry({
      'scene.asset.generate': {process: async () => undefined},
      'scene.avatar.generate': {process: async () => undefined}
    });

    await expect(registry.dispatch(job('scene.unknown.generate'))).rejects.toThrow(UnknownJobNameError);
  });
});
