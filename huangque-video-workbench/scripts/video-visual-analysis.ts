import {execFile} from 'node:child_process';
import {createHash} from 'node:crypto';

const frameWidth = 180;
const frameHeight = 320;

export type RegionMetrics = {
  standardDeviation: number;
  edgeScore: number;
  colorBins: number;
  fingerprint: string;
};

export type VideoFrameAnalysis = {
  full: RegionMetrics;
  title: RegionMetrics;
  asset: RegionMetrics;
  captions: RegionMetrics;
};

export class FixtureVisualError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'FixtureVisualError';
  }
}

export const fixtureVisualSampleTimes = (scenes: Array<{type: string; durationEstimate: number}>): {
  avatarTimeMs: number;
  brollTimeMs: number;
} => {
  let elapsedMs = 0;
  let avatarTimeMs: number | undefined;
  let brollTimeMs: number | undefined;
  for (const scene of scenes) {
    const durationMs = Math.round(scene.durationEstimate * 1000);
    const midpoint = elapsedMs + Math.max(1, Math.floor(durationMs / 2));
    if (scene.type === 'avatar' && avatarTimeMs === undefined) avatarTimeMs = midpoint;
    if (scene.type === 'image_video' && brollTimeMs === undefined) brollTimeMs = midpoint;
    elapsedMs += durationMs;
  }
  if (avatarTimeMs === undefined || brollTimeMs === undefined) {
    throw new FixtureVisualError('mixed fixture requires both avatar and B-roll sample intervals');
  }
  return {avatarTimeMs, brollTimeMs};
};

const extractFrame = (videoPath: string, timeMs: number): Promise<Buffer> => new Promise((resolve, reject) => {
  execFile('ffmpeg', [
    '-v', 'error', '-ss', (timeMs / 1000).toFixed(3), '-i', videoPath,
    '-frames:v', '1', '-vf', `scale=${frameWidth}:${frameHeight}`,
    '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'
  ], {encoding: 'buffer', maxBuffer: frameWidth * frameHeight * 4, windowsHide: true}, (error, stdout, stderr) => {
    if (error) {
      reject(new FixtureVisualError(`ffmpeg could not inspect ${videoPath}: ${stderr.toString().trim() || error.message}`));
      return;
    }
    const frame = Buffer.from(stdout);
    if (frame.length !== frameWidth * frameHeight * 3) {
      reject(new FixtureVisualError(`ffmpeg returned ${frame.length} bytes for ${videoPath}; expected ${frameWidth * frameHeight * 3}`));
      return;
    }
    resolve(frame);
  });
});

const metricsFor = (frame: Buffer, top: number, bottom: number): RegionMetrics => {
  const luminance: number[] = [];
  const colors = new Set<number>();
  const hash = createHash('sha256');
  for (let y = top; y < bottom; y += 1) {
    const rowStart = y * frameWidth * 3;
    const row = frame.subarray(rowStart, rowStart + frameWidth * 3);
    hash.update(row);
    for (let x = 0; x < frameWidth; x += 1) {
      const offset = x * 3;
      const red = row[offset] ?? 0;
      const green = row[offset + 1] ?? 0;
      const blue = row[offset + 2] ?? 0;
      luminance.push(0.2126 * red + 0.7152 * green + 0.0722 * blue);
      colors.add((red >> 5) << 6 | (green >> 5) << 3 | (blue >> 5));
    }
  }
  const mean = luminance.reduce((sum, value) => sum + value, 0) / luminance.length;
  const variance = luminance.reduce((sum, value) => sum + (value - mean) ** 2, 0) / luminance.length;
  let edgeTotal = 0;
  let edgeCount = 0;
  const regionHeight = bottom - top;
  for (let y = 0; y < regionHeight; y += 1) {
    for (let x = 0; x < frameWidth; x += 1) {
      const index = y * frameWidth + x;
      if (x + 1 < frameWidth) {
        edgeTotal += Math.abs((luminance[index] ?? 0) - (luminance[index + 1] ?? 0));
        edgeCount += 1;
      }
      if (y + 1 < regionHeight) {
        edgeTotal += Math.abs((luminance[index] ?? 0) - (luminance[index + frameWidth] ?? 0));
        edgeCount += 1;
      }
    }
  }
  return {
    standardDeviation: Math.sqrt(variance),
    edgeScore: edgeCount === 0 ? 0 : edgeTotal / edgeCount,
    colorBins: colors.size,
    fingerprint: hash.digest('hex')
  };
};

export const analyzeVideoFrame = async (videoPath: string, timeMs: number): Promise<VideoFrameAnalysis> => {
  const frame = await extractFrame(videoPath, timeMs);
  return {
    full: metricsFor(frame, 0, frameHeight),
    title: metricsFor(frame, 20, 64),
    asset: metricsFor(frame, 72, 212),
    captions: metricsFor(frame, 220, 278)
  };
};

export const assertDetailedFrame = (frame: VideoFrameAnalysis, label: string): void => {
  const failures: string[] = [];
  if (frame.full.standardDeviation <= 8) failures.push(`standard deviation ${frame.full.standardDeviation.toFixed(2)} <= 8`);
  if (frame.full.edgeScore <= 1.5) failures.push(`edge score ${frame.full.edgeScore.toFixed(2)} <= 1.5`);
  if (frame.full.colorBins <= 12) failures.push(`color bins ${frame.full.colorBins} <= 12`);
  if (failures.length > 0) throw new FixtureVisualError(`${label} is visually uniform: ${failures.join(', ')}`);
};

export const assertFixtureVideoVisuals = async ({
  videoPath,
  avatarTimeMs,
  brollTimeMs
}: {
  videoPath: string;
  avatarTimeMs: number;
  brollTimeMs: number;
}): Promise<{avatar: VideoFrameAnalysis; broll: VideoFrameAnalysis}> => {
  const avatar = await analyzeVideoFrame(videoPath, avatarTimeMs);
  const broll = await analyzeVideoFrame(videoPath, brollTimeMs);
  assertDetailedFrame(avatar, 'avatar interval');
  assertDetailedFrame(broll, 'B-roll interval');

  for (const [label, frame] of [['avatar', avatar], ['B-roll', broll]] as const) {
    if (frame.title.standardDeviation <= 8 || frame.title.edgeScore <= 1.5) {
      throw new FixtureVisualError(`${label} title region does not contain visible title detail`);
    }
    if (frame.captions.standardDeviation <= 8 || frame.captions.edgeScore <= 1.5) {
      throw new FixtureVisualError(`${label} caption region does not contain visible caption detail`);
    }
  }
  if (avatar.asset.fingerprint === broll.asset.fingerprint) {
    throw new FixtureVisualError('avatar and B-roll intervals contain the same central asset pixels');
  }
  return {avatar, broll};
};
