import {expect, test} from '@playwright/test';
import {resolve} from 'node:path';
import {assertFixtureVideoVisuals, fixtureVisualSampleTimes} from '../../scripts/video-visual-analysis.js';

const fixtureScript = '???????????????????????????????????????';

test('script becomes a completed mixed-scene preview', async ({page}) => {
  await page.goto('/projects/new');
  await page.getByLabel('Script').fill(fixtureScript);
  await page.getByLabel('Avatar').fill('mock');
  await page.getByLabel('Voice').fill('mock');
  await page.getByLabel('Template').fill('vertical_knowledge_v1');
  await page.getByRole('button', {name: 'Create project'}).click();

  await expect(page).toHaveURL(/\/projects\/[A-Za-z0-9_-]+$/);
  await expect(page.getByRole('heading', {name: 'Project progress'})).toBeVisible();
  const progress = page.locator('output[aria-live="polite"]');
  await expect(progress).toHaveText(/^(?:CREATED|STORYBOARDING|GENERATING_ASSETS|GENERATING_AVATAR|ALIGNING_TIMELINE|RENDERING|QUALITY_CHECK)$/u);
  await expect(page.getByText('COMPLETED', {exact: true})).toBeVisible({timeout: 120_000});

  const project = await page.evaluate(async () => {
    const response = await fetch(`/api${window.location.pathname}`);
    if (!response.ok) throw new Error(`project request failed with ${response.status}`);
    return response.json() as Promise<{
      scenes: Array<{id: string; visual: {workerScene?: {type?: string; durationEstimate?: number}}}>;
      assetVersions: Array<{sceneId: string; uri: string}>;
    }>;
  });
  const sceneTypes = project.scenes.map((scene) => scene.visual.workerScene?.type);
  expect(sceneTypes).toContain('avatar');
  expect(sceneTypes).toContain('image_video');
  expect(project.assetVersions).toHaveLength(project.scenes.length);
  const sceneIdsWithAssets = new Set(project.assetVersions.map((asset) => asset.sceneId));
  expect(project.scenes.every((scene) => sceneIdsWithAssets.has(scene.id))).toBe(true);
  expect(project.assetVersions.every((asset) => asset.uri.startsWith('file:'))).toBe(true);

  const preview = page.getByLabel('Project preview');
  await expect(preview).toHaveAttribute('src', /\/preview\.mp4$/);
  const previewUrl = await preview.getAttribute('src');
  const previewResponse = await page.request.get(previewUrl!);
  expect(previewResponse.status()).toBe(200);
  expect(previewResponse.headers()['content-type']).toContain('video/mp4');
  expect((await previewResponse.body()).subarray(4, 8).toString('ascii')).toBe('ftyp');

  const sampleTimes = fixtureVisualSampleTimes(project.scenes.map((scene) => ({
    type: scene.visual.workerScene?.type ?? '',
    durationEstimate: scene.visual.workerScene?.durationEstimate ?? 0
  })));
  await assertFixtureVideoVisuals({
    videoPath: resolve('tests', 'output', 'final.mp4'),
    ...sampleTimes
  });
});
