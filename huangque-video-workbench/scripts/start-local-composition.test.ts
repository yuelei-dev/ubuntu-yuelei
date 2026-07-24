import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {describe, expect, it, vi} from 'vitest';
import type {PipelineScene} from '../apps/worker/src/pipeline.js';
import {buildRendererProps, InProcessFixtureQueue} from './start-local-composition.js';

const scene = (id: string, type: 'avatar' | 'image_video', uri: string): PipelineScene => ({
  id,
  projectId: 'project_123',
  order: type === 'avatar' ? 1 : 2,
  type,
  purpose: 'fixture',
  script: `${type} fixture`,
  durationEstimate: 1,
  visual: {layout: 'full', highlightWords: []},
  status: 'READY',
  assetVersions: [{
    uri,
    width: 1080,
    height: 1920,
    provenance: 'generated',
    inputHash: id.repeat(64).slice(0, 64)
  }]
});

describe('local composition', () => {
  it('passes the generated avatar and image assets into Remotion', async () => {
    const props = await buildRendererProps([
      scene('a', 'avatar', pathToFileURL(resolve('tests/fixtures/avatar-source.mp4')).href),
      scene('b', 'image_video', pathToFileURL(resolve('tests/fixtures/product.jpg')).href)
    ]);

    expect(props.scenes[0]).toMatchObject({layout: 'avatar_full', assetMediaKind: 'image'});
    expect(props.scenes[0]?.assetUri).toMatch(/^data:image\/png;base64,/u);
    expect(props.scenes[1]).toMatchObject({layout: 'visual_full', assetMediaKind: 'image'});
    expect(props.scenes[1]?.assetUri).toMatch(/^data:image\/jpeg;base64,/u);
    expect(props.timeline.scenes.every((timelineScene) => (timelineScene.words?.length ?? 0) > 0)).toBe(true);
  });

  it('reports a processor error after its final bounded attempt', async () => {
    const stderr = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    try {
      const onTerminalFailure = vi.fn(async () => undefined);
      const queue = new InProcessFixtureQueue(onTerminalFailure);
      queue.setProcessor(async () => { throw new Error('renderer unavailable'); });

      await queue.submit({
        id: 'job_123',
        name: 'storyboard.generate',
        data: {projectId: 'project_123', sceneId: '__project__'},
        options: {attempts: 2, backoff: {type: 'exponential', delay: 1}}
      });
      await queue.close();

      expect(onTerminalFailure).toHaveBeenCalledOnce();
      expect(onTerminalFailure).toHaveBeenCalledWith(
        expect.objectContaining({id: 'job_123'}),
        expect.objectContaining({message: 'renderer unavailable'})
      );
    } finally {
      stderr.mockRestore();
    }
  });
});
