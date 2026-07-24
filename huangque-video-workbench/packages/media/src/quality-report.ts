import {mkdir, rename, rm, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {runMediaCommand} from './command.js';
import {validateMediaPath} from './paths.js';
import {MediaProbeError, probeMedia, type MediaProbe} from './probe.js';

export type QualityExpectation = {
  width: number;
  height: number;
  durationMs: number;
  durationToleranceMs?: number;
  expectedFrameRate?: number;
  frameRateTolerance?: number;
  maxBlackFrameRatio?: number;
  maxSilenceRatio?: number;
};

export type QualityReport = {
  passed: boolean;
  errors: string[];
  probe?: MediaProbe;
  metrics: {
    durationDriftMs?: number;
    frameRate?: number;
    blackFrameRatio?: number;
    silenceRatio?: number;
  };
};

export class QualityGateError extends Error {
  constructor(readonly report: QualityReport) {
    super(`media quality check failed: ${report.errors.join(', ')}`);
    this.name = 'QualityGateError';
  }
}

export type QualityGateResult =
  | {ok: true; report: QualityReport}
  | {ok: false; report: QualityReport; error: QualityGateError};

const ratioFromLog = (log: string, name: 'black_duration' | 'silence_duration', durationMs: number): number => {
  if (durationMs <= 0) return 0;
  const matches = [...log.matchAll(new RegExp(`${name}:\\s*([0-9.]+)`, 'g'))];
  const totalSeconds = matches.reduce((total, match) => total + Number(match[1]), 0);
  return Number.isFinite(totalSeconds) ? totalSeconds * 1000 / durationMs : 0;
};

const inspectBlackFrames = async (path: string, durationMs: number, signal?: AbortSignal): Promise<number> => {
  const {stderr} = await runMediaCommand('ffmpeg', ['-nostdin', '-v', 'info', '-i', path, '-an', '-vf', 'blackdetect=d=0.1:pic_th=0.98:pix_th=0.10', '-f', 'null', '-'], {signal});
  return ratioFromLog(stderr, 'black_duration', durationMs);
};

const inspectSilence = async (path: string, durationMs: number, signal?: AbortSignal): Promise<number> => {
  const {stderr} = await runMediaCommand('ffmpeg', ['-nostdin', '-v', 'info', '-i', path, '-vn', '-af', 'silencedetect=noise=-50dB:d=0.5', '-f', 'null', '-'], {signal});
  return ratioFromLog(stderr, 'silence_duration', durationMs);
};

export const qualityReportPath = (): string => resolve(process.cwd(), 'reports', 'quality.json');

const writeReport = async (report: QualityReport, destination: string): Promise<void> => {
  await mkdir(dirname(destination), {recursive: true});
  const temporary = `${destination}.huangque-${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    await rename(temporary, destination);
  } finally {
    await rm(temporary, {force: true});
  }
};

export const inspectOutput = async (
  path: string,
  expected: QualityExpectation,
  options: {reportPath?: string; signal?: AbortSignal} = {}
): Promise<QualityReport> => {
  const errors: string[] = [];
  const metrics: QualityReport['metrics'] = {};
  let probe: MediaProbe | undefined;
  try {
    probe = await probeMedia(path, options.signal);
  } catch (error) {
    if (error instanceof MediaProbeError) errors.push('media is damaged or unprobeable');
    else throw error;
  }

  if (probe) {
    if (probe.video?.codecName !== 'h264') errors.push('video codec mismatch');
    if (!probe.audio) errors.push('audio stream missing');
    else if (probe.audio.codecName !== 'aac') errors.push('audio codec mismatch');
    if (probe.video?.width !== expected.width || probe.video?.height !== expected.height) errors.push('resolution mismatch');
    metrics.frameRate = probe.video?.frameRate;
    if (probe.video?.frameRate === undefined || Math.abs(probe.video.frameRate - (expected.expectedFrameRate ?? 30)) > (expected.frameRateTolerance ?? 0.1)) {
      errors.push('frame rate mismatch');
    }

    const durationDriftMs = Math.abs(probe.durationMs - expected.durationMs);
    metrics.durationDriftMs = durationDriftMs;
    if (durationDriftMs > (expected.durationToleranceMs ?? 100)) errors.push('duration drift exceeds 100ms');

    try {
      metrics.blackFrameRatio = await inspectBlackFrames(validateMediaPath(path), probe.durationMs, options.signal);
      if (metrics.blackFrameRatio > (expected.maxBlackFrameRatio ?? 0.5)) errors.push('black-frame ratio exceeds allowed threshold');
      if (probe.audio) {
        metrics.silenceRatio = await inspectSilence(validateMediaPath(path), probe.durationMs, options.signal);
        if (metrics.silenceRatio > (expected.maxSilenceRatio ?? 0.8)) errors.push('unexpected silence ratio exceeds allowed threshold');
      }
    } catch {
      errors.push('quality analysis failed');
    }
  }

  const report: QualityReport = {passed: errors.length === 0, errors, ...(probe ? {probe} : {}), metrics};
  await writeReport(report, resolve(options.reportPath ?? qualityReportPath()));
  return report;
};

export const qualityGate = (report: QualityReport): QualityGateResult => report.passed
  ? {ok: true, report}
  : {ok: false, report, error: new QualityGateError(report)};

export const requirePassingQuality = (report: QualityReport): QualityReport => {
  const result = qualityGate(report);
  if (!result.ok) throw result.error;
  return result.report;
};
