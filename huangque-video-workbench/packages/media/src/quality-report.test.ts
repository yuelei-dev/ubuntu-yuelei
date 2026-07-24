import {execFile} from 'node:child_process';
import {mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {promisify} from 'node:util';
import {afterAll, beforeAll, describe, expect, it} from 'vitest';
import {normalizeAudio} from './normalize-audio.js';
import {normalizeVideo} from './normalize-video.js';
import {probeMedia} from './probe.js';
import {inspectOutput, requirePassingQuality} from './quality-report.js';

const execFileAsync = promisify(execFile);
const fixtureDirectory = 'tests/fixtures/media';
const outputDirectory = 'tests/output/media';

const ffmpeg = async (arguments_: string[]): Promise<void> => {
  await execFileAsync('ffmpeg', ['-y', '-v', 'error', ...arguments_]);
};

beforeAll(async () => {
  await mkdir(fixtureDirectory, {recursive: true});
  await mkdir(outputDirectory, {recursive: true});
  await ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', `${fixtureDirectory}/source.mp4`]);
  await ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', `${fixtureDirectory}/wrong-size.mp4`]);
  await ffmpeg(['-f', 'lavfi', '-i', 'color=c=black:size=1080x1920:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', `${fixtureDirectory}/black.mp4`]);
  await ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=1080x1920:rate=30', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', `${fixtureDirectory}/silent.mp4`]);
  await ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24', '-t', '1', '-c:v', 'libx264', '-an', `${fixtureDirectory}/video-only-24fps.mp4`]);
  await writeFile(`${fixtureDirectory}/corrupt.mp4`, 'not media');
});

afterAll(async () => {
  await rm(outputDirectory, {recursive: true, force: true});
  await rm(fixtureDirectory, {recursive: true, force: true});
});

describe('media quality inspection', () => {
  it('fails output with the wrong resolution or duration drift above 100ms', async () => {
    const report = await inspectOutput(`${fixtureDirectory}/wrong-size.mp4`, {width: 1080, height: 1920, durationMs: 5000});

    expect(report.passed).toBe(false);
    expect(report.errors).toContain('resolution mismatch');
    expect(report.errors).toContain('duration drift exceeds 100ms');
  });

  it('normalizes video to a vertical H.264/AAC output and probes its streams', async () => {
    const output = `${outputDirectory}/normalized.mp4`;
    await normalizeVideo(`${fixtureDirectory}/source.mp4`, output);

    const probe = await probeMedia(output);
    expect(probe.video).toMatchObject({codecName: 'h264', width: 1080, height: 1920, frameRate: 30});
    expect(probe.audio).toMatchObject({codecName: 'aac'});
  });

  it('normalizes standalone audio with loudness filtering', async () => {
    const output = `${outputDirectory}/normalized-audio.m4a`;
    await normalizeAudio(`${fixtureDirectory}/source.mp4`, output);

    expect((await probeMedia(output)).audio).toMatchObject({codecName: 'aac'});
  });

  it('adds normalized AAC audio when the input contains only video', async () => {
    const output = `${outputDirectory}/video-only-normalized.mp4`;
    await normalizeVideo(`${fixtureDirectory}/video-only-24fps.mp4`, output);

    expect((await probeMedia(output)).audio).toMatchObject({codecName: 'aac'});
  });

  it('fails a 24fps output when 30fps is expected within tolerance', async () => {
    const report = await inspectOutput(`${fixtureDirectory}/video-only-24fps.mp4`, {width: 640, height: 360, durationMs: 1000, expectedFrameRate: 30, frameRateTolerance: 0.1});

    expect(report.errors).toContain('frame rate mismatch');
  });

  it('flags black frames and unexpected silence', async () => {
    const blackReport = await inspectOutput(`${fixtureDirectory}/black.mp4`, {width: 1080, height: 1920, durationMs: 1000});
    const silentReport = await inspectOutput(`${fixtureDirectory}/silent.mp4`, {width: 1080, height: 1920, durationMs: 1000});

    expect(blackReport.errors).toContain('black-frame ratio exceeds allowed threshold');
    expect(silentReport.errors).toContain('unexpected silence ratio exceeds allowed threshold');
  });

  it('returns a failed typed boundary for damaged media', async () => {
    const report = await inspectOutput(`${fixtureDirectory}/corrupt.mp4`, {width: 1080, height: 1920, durationMs: 1000});

    expect(report.errors).toContain('media is damaged or unprobeable');
    expect(() => requirePassingQuality(report)).toThrow('media quality check failed');
  });

  it('writes a report to a caller-scoped path', async () => {
    const reportPath = `${outputDirectory}/project-123/quality.json`;
    const report = await inspectOutput(
      `${fixtureDirectory}/source.mp4`,
      {width: 640, height: 360, durationMs: 1000, expectedFrameRate: 24},
      {reportPath}
    );

    expect(JSON.parse(await readFile(reportPath, 'utf8'))).toEqual(report);
  });
});
