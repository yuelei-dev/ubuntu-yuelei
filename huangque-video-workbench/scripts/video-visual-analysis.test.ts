import {execFile} from 'node:child_process';
import {mkdir, rm} from 'node:fs/promises';
import {resolve} from 'node:path';
import {promisify} from 'node:util';
import {afterAll, beforeAll, describe, expect, it} from 'vitest';
import {assertDetailedFrame, analyzeVideoFrame, analyzeVideoFrameWithinDeadline, fixtureVisualSampleTimes, FixtureVisualError} from './video-visual-analysis.js';
import {FixtureTimeoutError} from './deadline.js';

const execFileAsync = promisify(execFile);
const outputDirectory = resolve('tests', 'output', 'visual-analysis');
const uniformVideo = resolve(outputDirectory, 'uniform.mp4');
const detailedVideo = resolve(outputDirectory, 'detailed.mp4');

describe('fixture video visual analysis', () => {
  beforeAll(async () => {
    await mkdir(outputDirectory, {recursive: true});
    await execFileAsync('ffmpeg', ['-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=#2563eb:s=180x320:r=30:d=1', '-pix_fmt', 'yuv420p', uniformVideo]);
    await execFileAsync('ffmpeg', ['-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=180x320:r=30:d=1', '-pix_fmt', 'yuv420p', detailedVideo]);
  });

  afterAll(async () => { await rm(outputDirectory, {recursive: true, force: true}); });

  it('rejects a spatially uniform frame extracted through ffmpeg', async () => {
    const frame = await analyzeVideoFrame(uniformVideo, 500);

    expect(frame.full.standardDeviation).toBeLessThan(1);
    expect(frame.full.edgeScore).toBeLessThan(1);
    expect(() => assertDetailedFrame(frame, 'uniform fixture')).toThrow(FixtureVisualError);
  });

  it('accepts a frame with spatial variance, edges, and color diversity', async () => {
    const frame = await analyzeVideoFrame(detailedVideo, 500);

    expect(frame.full.standardDeviation).toBeGreaterThan(10);
    expect(frame.full.edgeScore).toBeGreaterThan(2);
    expect(frame.full.colorBins).toBeGreaterThan(16);
    expect(() => assertDetailedFrame(frame, 'detailed fixture')).not.toThrow();
  });

  it('bounds a never-settling visual frame analysis', async () => {
    await expect(analyzeVideoFrameWithinDeadline(
      detailedVideo,
      500,
      new AbortController().signal,
      10,
      async () => await new Promise(() => undefined)
    )).rejects.toMatchObject<Partial<FixtureTimeoutError>>({
      name: 'FixtureTimeoutError',
      operation: 'fixture visual frame analysis',
      timeoutMs: 10
    });
  });

  it('selects midpoints from distinct avatar and B-roll intervals', () => {
    expect(fixtureVisualSampleTimes([
      {type: 'avatar', durationEstimate: 2},
      {type: 'image_video', durationEstimate: 3}
    ])).toEqual({avatarTimeMs: 1000, brollTimeMs: 3500});
  });
});
