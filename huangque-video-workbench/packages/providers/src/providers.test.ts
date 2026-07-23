import {describe, expect, it} from 'vitest';
import {assertAllowedProvenance} from './provenance.js';
import {MockAvatarProvider} from './mock/avatar.js';
import {MockImageProvider} from './mock/image.js';
import {MockVideoProvider} from './mock/video.js';
import {copyFixture} from './mock/shared.js';

describe('assertAllowedProvenance', () => {
  it('rejects generated media for factual evidence scenes', () => {
    expect(() => assertAllowedProvenance(
      {type: 'screenshot', asset: {source: 'generate', factual: true}},
      {uri: 'mock://image', width: 1080, height: 1920, provenance: 'generated', inputHash: 'test'}
    )).toThrow('factual scenes require uploaded or verified assets');
  });

  it('rejects fallback media for factual evidence scenes while allowing verified and non-factual assets', () => {
    const factualScreenshot = {type: 'screenshot', asset: {source: 'upload', factual: true}};

    expect(() => assertAllowedProvenance(
      factualScreenshot,
      {uri: 'mock://fallback', width: 1080, height: 1920, provenance: 'fallback', inputHash: 'test'}
    )).toThrow('factual scenes require uploaded or verified assets');
    expect(() => assertAllowedProvenance(
      factualScreenshot,
      {uri: 'mock://enterprise', width: 1080, height: 1920, provenance: 'enterprise', inputHash: 'test'}
    )).not.toThrow();
    expect(() => assertAllowedProvenance(
      {type: 'image', asset: {source: 'generate', factual: false}},
      {uri: 'mock://generated', width: 1080, height: 1920, provenance: 'generated', inputHash: 'test'}
    )).not.toThrow();
  });
});

describe('offline providers', () => {
  it('copies local fixtures and hashes equivalent avatar inputs deterministically', async () => {
    const provider = new MockAvatarProvider();
    const request = {projectId: 'project_001', sceneId: 'scene_001', text: 'Hello', width: 1080, height: 1920};

    const first = await provider.generate(request);
    const second = await provider.generate(request);

    expect(first).toMatchObject({width: 1080, height: 1920, provenance: 'generated'});
    expect(first.inputHash).toMatch(/^[a-f0-9]{64}$/);
    expect(second.inputHash).toBe(first.inputHash);
    expect(second.uri).toBe(first.uri);
    expect(first.uri).toMatch(/huangque-video-workbench/);
  });

  it('uses canonical key ordering for equivalent request hashes', async () => {
    const provider = new MockAvatarProvider();
    const first = await provider.generate({projectId: 'project_001', sceneId: 'scene_001', text: 'Hello', width: 1080, height: 1920});
    const second = await provider.generate({height: 1920, text: 'Hello', width: 1080, sceneId: 'scene_001', projectId: 'project_001'});

    expect(second.inputHash).toBe(first.inputHash);
    expect(second.uri).toBe(first.uri);
  });

  it('separates otherwise identical mock outputs by project', async () => {
    const provider = new MockImageProvider();
    const commonRequest = {sceneId: 'scene_002', prompt: 'blue product', width: 1080, height: 1920};
    const first = await provider.generate({projectId: 'project-alpha', ...commonRequest});
    const second = await provider.generate({projectId: 'project-beta', ...commonRequest});

    expect(first.uri).toContain('project-alpha');
    expect(second.uri).toContain('project-beta');
    expect(second.uri).not.toBe(first.uri);
  });

  it('rejects traversal values before copying fixtures', async () => {
    await expect(copyFixture({projectId: '../outside', fixture: 'avatar', inputHash: 'a'.repeat(64)})).rejects.toThrow('invalid project identifier');
    await expect(copyFixture({projectId: 'project_001', fixture: '../product.jpg' as 'avatar', inputHash: 'a'.repeat(64)})).rejects.toThrow('unknown fixture');
    await expect(copyFixture({projectId: 'project_001', fixture: 'toString' as 'avatar', inputHash: 'a'.repeat(64)})).rejects.toThrow('unknown fixture');
  });

  it('gives different input hashes to distinct image and video requests', async () => {
    const image = new MockImageProvider();
    const video = new MockVideoProvider();

    const firstImage = await image.generate({projectId: 'project_001', sceneId: 'scene_002', prompt: 'blue product', width: 1080, height: 1920});
    const secondImage = await image.generate({projectId: 'project_001', sceneId: 'scene_002', prompt: 'red product', width: 1080, height: 1920});
    const generatedVideo = await video.generate({projectId: 'project_001', sceneId: 'scene_003', prompt: 'product rotation', width: 1080, height: 1920, durationMs: 3000});

    expect(firstImage.inputHash).not.toBe(secondImage.inputHash);
    expect(generatedVideo).toMatchObject({width: 1080, height: 1920, durationMs: 3000, provenance: 'generated'});
  });
});
