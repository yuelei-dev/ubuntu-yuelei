import {assertReadableMediaPath} from './paths.js';
import {MediaCommandError, runMediaCommand} from './command.js';

export type MediaStream = {
  codecType: 'video' | 'audio' | 'other';
  codecName: string;
  width?: number;
  height?: number;
  frameRate?: number;
};

export type MediaProbe = {
  path: string;
  durationMs: number;
  streams: MediaStream[];
  video?: MediaStream;
  audio?: MediaStream;
};

export class MediaProbeError extends Error {
  constructor(message: string, readonly cause?: unknown) {
    super(message);
    this.name = 'MediaProbeError';
  }
}

type ProbeJson = {
  format?: {duration?: string};
  streams?: Array<{codec_type?: string; codec_name?: string; width?: number; height?: number; avg_frame_rate?: string; r_frame_rate?: string}>;
};

const parseFrameRate = (value: string | undefined): number | undefined => {
  if (!value || value === '0/0') return undefined;
  const [numerator, denominator] = value.split('/').map(Number);
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator === 0) return undefined;
  return numerator / denominator;
};

const parseProbe = (path: string, output: string): MediaProbe => {
  let parsed: ProbeJson;
  try {
    parsed = JSON.parse(output) as ProbeJson;
  } catch (error) {
    throw new MediaProbeError('ffprobe returned invalid JSON', error);
  }

  const durationSeconds = Number(parsed.format?.duration);
  if (!Number.isFinite(durationSeconds) || durationSeconds < 0 || !Array.isArray(parsed.streams)) {
    throw new MediaProbeError('ffprobe response is missing usable media metadata');
  }

  const streams = parsed.streams.map((stream): MediaStream => ({
    codecType: stream.codec_type === 'video' || stream.codec_type === 'audio' ? stream.codec_type : 'other',
    codecName: stream.codec_name ?? 'unknown',
    ...(typeof stream.width === 'number' ? {width: stream.width} : {}),
    ...(typeof stream.height === 'number' ? {height: stream.height} : {}),
    ...(parseFrameRate(stream.avg_frame_rate ?? stream.r_frame_rate) !== undefined ? {frameRate: parseFrameRate(stream.avg_frame_rate ?? stream.r_frame_rate)} : {})
  }));
  const video = streams.find((stream) => stream.codecType === 'video');
  const audio = streams.find((stream) => stream.codecType === 'audio');

  return {path, durationMs: Math.round(durationSeconds * 1000), streams, ...(video ? {video} : {}), ...(audio ? {audio} : {})};
};

export const probeMedia = async (path: string, signal?: AbortSignal): Promise<MediaProbe> => {
  try {
    const input = await assertReadableMediaPath(path);
    const {stdout} = await runMediaCommand('ffprobe', ['-v', 'error', '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate', '-of', 'json', input], {signal});
    return parseProbe(input, stdout);
  } catch (error) {
    if (error instanceof MediaProbeError) throw error;
    if (error instanceof MediaCommandError) throw new MediaProbeError('ffprobe could not inspect media', error);
    throw new MediaProbeError('media path could not be inspected', error);
  }
};
