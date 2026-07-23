import {execFile} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {existsSync, createReadStream} from 'node:fs';
import {copyFile, mkdir, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {promisify} from 'node:util';
import type {AddressInfo} from 'node:net';
import {makeCancelSignal, openBrowser, renderMedia, selectComposition} from '@remotion/renderer';
import {buildStoryboard} from '@huangque/director';
import {inspectOutput} from '@huangque/media';
import {MockAvatarProvider, MockImageProvider} from '@huangque/providers';
import {buildTimeline} from '@huangque/timeline';
import {
  InMemoryProjectEventBroker,
  InMemoryProjectRepository,
  type QueueAdapter,
  type QueueJob
} from '@huangque/api';
import {createApp} from '../apps/api/src/app.js';
import {createBullMqPipelineProcessor, type BullMqSceneDelivery} from '../apps/worker/src/bullmq-worker.js';
import {Task4PipelineRepository, type PipelineScene} from '../apps/worker/src/pipeline.js';
import type {VerticalKnowledgeVideoProps} from '../packages/renderer/src/types.js';
import {withDeadline} from './deadline.js';
import {assertFixtureVideoVisuals, fixtureVisualSampleTimes} from './video-visual-analysis.js';

const execFileAsync = promisify(execFile);
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const terminalQueueRetries = 3;
export const fixtureDeadlines = {
  bundleMs: 60_000,
  compositionStartupMs: 30_000,
  renderMs: 90_000,
  muxMs: 30_000,
  queueDrainMs: 10_000,
  shutdownMs: 10_000
} as const;

export const resolveChromeExecutable = (): string => {
  const configured = process.env.HUANGQUE_CHROME_PATH;
  const candidates = [
    configured,
    process.env.LOCALAPPDATA && resolve(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.PROGRAMFILES && resolve(process.env.PROGRAMFILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['PROGRAMFILES(X86)'] && resolve(process.env['PROGRAMFILES(X86)'], 'Google', 'Chrome', 'Application', 'chrome.exe')
  ].filter((candidate): candidate is string => Boolean(candidate));
  const executable = candidates.find((candidate) => existsSync(candidate));
  if (!executable) throw new Error('Google Chrome was not found; set HUANGQUE_CHROME_PATH to chrome.exe');
  return executable;
};

const asDelivery = (job: QueueJob, attemptsMade: number): BullMqSceneDelivery => ({
  name: job.name,
  data: {
    projectId: job.data.projectId as string,
    sceneId: job.data.sceneId as string,
    businessJobId: job.id,
    input: job.data.input
  },
  attemptsMade
});

export class InProcessFixtureQueue implements QueueAdapter {
  private readonly known = new Set<string>();
  private readonly pending: QueueJob[] = [];
  private processor?: (delivery: BullMqSceneDelivery) => Promise<void>;
  private pumpPromise?: Promise<void>;
  private closed = false;
  private readonly controller = new AbortController();

  get signal(): AbortSignal { return this.controller.signal; }

  constructor(private readonly onTerminalFailure: (job: QueueJob, error: Error) => Promise<void> = async () => undefined) {}

  setProcessor(processor: (delivery: BullMqSceneDelivery) => Promise<void>): void {
    this.processor = processor;
  }

  async submit(job: QueueJob): Promise<{id: string; existing: boolean}> {
    if (this.closed) throw new Error('fixture queue is closed');
    if (this.known.has(job.id)) return {id: job.id, existing: true};
    this.known.add(job.id);
    this.pending.push(structuredClone(job));
    void this.pump();
    return {id: job.id, existing: false};
  }

  private pump(): Promise<void> {
    if (this.pumpPromise) return this.pumpPromise;
    this.pumpPromise = (async () => {
      while (this.pending.length > 0) {
        const job = this.pending.shift()!;
        const attempts = job.options?.attempts ?? terminalQueueRetries;
        for (let attemptsMade = 0; attemptsMade < attempts; attemptsMade += 1) {
          try {
            if (!this.processor) throw new Error('fixture queue processor is not configured');
            await this.processor(asDelivery(job, attemptsMade));
            break;
          } catch (error) {
            if (attemptsMade + 1 >= attempts || (error as {name?: string}).name === 'UnrecoverableError') {
              const terminalError = error instanceof Error ? error : new Error(String(error));
              process.stderr.write(`fixture job ${job.name} failed: ${terminalError.stack ?? terminalError.message}\n`);
              await this.onTerminalFailure(job, terminalError);
              break;
            }
          }
        }
      }
    })().finally(() => {
      this.pumpPromise = undefined;
      if (this.pending.length > 0 && !this.closed) void this.pump();
    });
    return this.pumpPromise;
  }

  async close(): Promise<void> {
    this.closed = true;
    await this.pumpPromise;
  }

  abort(reason = new Error('fixture queue aborted')): void {
    this.closed = true;
    this.pending.length = 0;
    this.controller.abort(reason);
  }
}

export const prepareFixtureBundles = async (runDirectory: string, signal: AbortSignal): Promise<{clientBundle: string; serveUrl: string}> => {
  const cli = resolve(repositoryRoot, 'node_modules', 'tsx', 'dist', 'cli.mjs');
  const script = resolve(repositoryRoot, 'scripts', 'prepare-fixture-bundles.ts');
  const {stdout} = await execFileAsync(process.execPath, [cli, script, repositoryRoot, runDirectory], {
    cwd: repositoryRoot, encoding: 'utf8', maxBuffer: 1024 * 1024, signal, windowsHide: true
  });
  const lastLine = stdout.trim().split(/\r?\n/u).at(-1);
  if (!lastLine) throw new Error('fixture bundle process returned no result');
  const result = JSON.parse(lastLine) as {clientBundlePath?: unknown; serveUrl?: unknown};
  if (typeof result.clientBundlePath !== 'string' || typeof result.serveUrl !== 'string') {
    throw new Error('fixture bundle process returned malformed paths');
  }
  return {clientBundle: await readFile(result.clientBundlePath, 'utf8'), serveUrl: result.serveUrl};
};

const avatarFrameDataUri = (assetPath: string, signal: AbortSignal): Promise<string> => new Promise((resolveFrame, rejectFrame) => {
  execFile('ffmpeg', [
    '-v', 'error', '-ss', '0.250', '-i', assetPath, '-frames:v', '1',
    '-vf', 'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920',
    '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'
  ], {encoding: 'buffer', maxBuffer: 16 * 1024 * 1024, signal, windowsHide: true}, (error, stdout, stderr) => {
    if (error) {
      rejectFrame(new Error(`could not extract avatar frame: ${stderr.toString().trim() || error.message}`));
      return;
    }
    resolveFrame(`data:image/png;base64,${Buffer.from(stdout).toString('base64')}`);
  });
});

const fixtureAssetDataUri = async (scene: PipelineScene, parentSignal?: AbortSignal): Promise<string | undefined> => {
  const asset = scene.assetVersions.at(-1);
  if (!asset) return undefined;
  if (asset.uri.startsWith('data:')) return asset.uri;
  const assetUrl = new URL(asset.uri);
  if (assetUrl.protocol !== 'file:') {
    throw new Error(`fixture scene ${scene.id} produced unsupported asset URI protocol ${assetUrl.protocol}`);
  }
  const assetPath = fileURLToPath(assetUrl);
  if (scene.type === 'avatar') {
    return withDeadline('avatar fixture frame extraction', 15_000, (deadlineSignal) =>
      avatarFrameDataUri(assetPath, parentSignal ? AbortSignal.any([parentSignal, deadlineSignal]) : deadlineSignal));
  }
  parentSignal?.throwIfAborted();
  const image = await readFile(assetPath);
  parentSignal?.throwIfAborted();
  return `data:image/jpeg;base64,${image.toString('base64')}`;
};

const captionWords = (script: string, durationMs: number) => {
  const words = script.trim().split(/\s+/u).filter(Boolean);
  const step = durationMs / Math.max(words.length, 1);
  return words.map((text, index) => ({
    text,
    startMs: Math.round(index * step),
    endMs: index + 1 === words.length ? durationMs : Math.round((index + 1) * step)
  }));
};

export const buildRendererProps = async (scenes: PipelineScene[], signal?: AbortSignal): Promise<VerticalKnowledgeVideoProps> => ({
  timeline: buildTimeline({
    fps: 30,
    scenes: scenes.map((scene) => {
      const audioDurationMs = Math.round(scene.durationEstimate * 1000);
      return {id: scene.id, audioDurationMs, words: captionWords(scene.script, audioDurationMs)};
    })
  }),
  scenes: await Promise.all(scenes.map(async (scene) => ({
    id: scene.id,
    layout: scene.type === 'avatar' ? 'avatar_full' : 'visual_full',
    script: scene.script,
    headline: scene.script,
    highlightWords: scene.visual.highlightWords,
    assetUri: await fixtureAssetDataUri(scene, signal),
    assetMediaKind: 'image'
  })))
});

const renderFixtureVideoWithinDeadline = async ({
  projectId,
  scenes,
  runDirectory,
  chromeExecutable,
  serveUrl,
  signal
}: {
  projectId: string;
  scenes: PipelineScene[];
  runDirectory: string;
  chromeExecutable: string;
  serveUrl: string;
  signal: AbortSignal;
}): Promise<{outputPath: string; durationMs: number}> => {
  const projectDirectory = resolve(runDirectory, projectId);
  const silentOutput = resolve(projectDirectory, 'composition.mp4');
  const outputPath = resolve(projectDirectory, 'preview.mp4');
  await mkdir(projectDirectory, {recursive: true});
  const props = await buildRendererProps(scenes, signal);
  const finalFrame = props.timeline.scenes.at(-1)?.endFrame;
  if (!finalFrame) throw new Error(`project ${projectId} has no renderable timeline`);
  const durationMs = Math.round(finalFrame / props.timeline.fps * 1000);
  let browser: Awaited<ReturnType<typeof openBrowser>> | undefined;
  try {
    const composition = await withDeadline('Remotion composition startup', fixtureDeadlines.compositionStartupMs, async () => {
      browser = await openBrowser('chrome', {browserExecutable: chromeExecutable, logLevel: 'warn'});
      return selectComposition({
        serveUrl,
        id: 'FixtureCanvasVideo',
        inputProps: props as unknown as Record<string, unknown>,
        puppeteerInstance: browser,
        timeoutInMilliseconds: fixtureDeadlines.compositionStartupMs,
        logLevel: 'warn'
      });
    }, async () => { await browser?.close({silent: true}); });
    const cancellation = makeCancelSignal();
    const abortRender = () => cancellation.cancel();
    signal.addEventListener('abort', abortRender, {once: true});
    try {
      await withDeadline('Remotion media render', fixtureDeadlines.renderMs, async () => renderMedia({
        serveUrl,
        composition,
        inputProps: props as unknown as Record<string, unknown>,
        codec: 'h264',
        outputLocation: silentOutput,
        puppeteerInstance: browser,
        cancelSignal: cancellation.cancelSignal,
        concurrency: 4,
        scale: 0.5,
        overwrite: true,
        timeoutInMilliseconds: fixtureDeadlines.renderMs,
        logLevel: 'warn'
      }), async () => { cancellation.cancel(); await browser?.close({silent: true}); });
    } finally {
      signal.removeEventListener('abort', abortRender);
    }
  } finally {
    await browser?.close({silent: true}).catch(() => undefined);
  }
  await withDeadline('ffmpeg fixture mux', fixtureDeadlines.muxMs, async (muxSignal) => {
    const combinedSignal = AbortSignal.any([signal, muxSignal]);
    await execFileAsync('ffmpeg', [
      '-y', '-nostdin', '-v', 'error',
      '-i', silentOutput,
      '-f', 'lavfi', '-i', `sine=frequency=660:sample_rate=48000:duration=${(durationMs / 1000).toFixed(3)}`,
      '-map', '0:v:0', '-map', '1:a:0',
      '-vf', 'scale=1080:1920:flags=lanczos,fps=30,format=yuv420p',
      '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
      '-c:a', 'aac', '-b:a', '160k',
      '-t', (durationMs / 1000).toFixed(3),
      '-movflags', '+faststart',
      outputPath
    ], {signal: combinedSignal, windowsHide: true});
  });
  return {outputPath, durationMs};
};

export const renderFixtureVideo = async (options: Parameters<typeof renderFixtureVideoWithinDeadline>[0]): Promise<{
  outputPath: string;
  durationMs: number;
}> => withDeadline('complete fixture render lifecycle', 150_000, (deadlineSignal) =>
  renderFixtureVideoWithinDeadline({...options, signal: AbortSignal.any([options.signal, deadlineSignal])}));

export type LocalComposition = {
  baseUrl: string;
  finalOutputPath: string;
  close(): Promise<void>;
};

export const startLocalComposition = async ({port = 4173}: {port?: number} = {}): Promise<LocalComposition> => {
  process.chdir(repositoryRoot);
  const runDirectory = resolve(tmpdir(), 'huangque-video-workbench', 'runs', randomUUID());
  const finalOutputPath = resolve(repositoryRoot, 'tests', 'output', 'final.mp4');
  const chromeExecutable = resolveChromeExecutable();
  await mkdir(runDirectory, {recursive: true});
  await mkdir(dirname(finalOutputPath), {recursive: true});

  let startupComplete = false;
  let cleanupStartup = async (): Promise<void> => undefined;
  try {
  const durableRepository = new InMemoryProjectRepository();
  const broker = new InMemoryProjectEventBroker();
  const workerRepository = new Task4PipelineRepository(durableRepository, () => new Date(), broker);
  const terminalProjectStatuses = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'NEEDS_USER_INPUT']);
  const queue = new InProcessFixtureQueue(async (job) => {
    const projectId = job.data.projectId;
    if (typeof projectId !== 'string') return;
    const project = await workerRepository.findProject(projectId);
    if (project && !terminalProjectStatuses.has(project.status)) {
      await workerRepository.updateProjectStatus(projectId, 'FAILED');
    }
  });
  const outputs = new Map<string, string>();
  const {clientBundle, serveUrl} = await withDeadline(
    'Vite and Remotion bundle preparation',
    fixtureDeadlines.bundleMs,
    (signal) => prepareFixtureBundles(runDirectory, signal)
  );
  const processor = createBullMqPipelineProcessor({
    repository: workerRepository,
    queue,
    buildStoryboard,
    avatarProvider: new MockAvatarProvider(),
    imageProvider: new MockImageProvider(),
    qualityInspector: {
      async inspect(project) {
        const scenes = await workerRepository.listScenes(project.id);
        const rendered = await renderFixtureVideo({projectId: project.id, scenes, runDirectory, chromeExecutable, serveUrl, signal: queue.signal});
        const reportPath = resolve(runDirectory, project.id, 'quality.json');
        const report = await inspectOutput(rendered.outputPath, {
          width: 1080,
          height: 1920,
          durationMs: rendered.durationMs,
          durationToleranceMs: 100,
          expectedFrameRate: 30
        }, {reportPath});
        const sampleTimes = fixtureVisualSampleTimes(scenes);
        await assertFixtureVideoVisuals({videoPath: rendered.outputPath, ...sampleTimes});
        if (report.passed) {
          outputs.set(project.id, rendered.outputPath);
          await copyFile(rendered.outputPath, finalOutputPath);
        }
        const previewUrl = `/api/projects/${project.id}/preview.mp4`;
        return {report, reportPath, previewUrl, downloadUrl: previewUrl};
      }
    }
  });
  queue.setProcessor(async (delivery) => {
    if (delivery.name === 'project.render') return;
    await processor(delivery);
  });

  const app = createApp({
    repository: durableRepository,
    queue,
    idFactory: () => `project_${randomUUID().replaceAll('-', '')}`,
    projectEvents: broker,
    heartbeatMs: 1_000
  });
  cleanupStartup = async () => {
    queue.abort(new Error('fixture composition startup failed'));
    app.server.closeAllConnections();
    await app.close().catch(() => undefined);
  };
  const html = '<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Huangque Video Workbench</title></head><body><div id="root"></div><script type="module" src="/fixture-client.js"></script></body></html>';
  app.get('/fixture-client.js', async (_request, reply) => reply.type('text/javascript; charset=utf-8').send(clientBundle));
  app.get('/favicon.ico', async (_request, reply) => reply.code(204).send());
  app.get<{Params: {id: string}}>('/api/projects/:id/preview.mp4', async (request, reply) => {
    const outputPath = outputs.get(request.params.id);
    if (!outputPath) return reply.code(404).send({error: 'preview_not_found'});
    return reply.type('video/mp4').header('cache-control', 'no-store').send(createReadStream(outputPath));
  });
  app.get('/', async (_request, reply) => reply.type('text/html; charset=utf-8').send(html));
  app.get('/projects/new', async (_request, reply) => reply.type('text/html; charset=utf-8').send(html));
  app.get<{Params: {id: string}}>('/projects/:id', async (_request, reply) => reply.type('text/html; charset=utf-8').send(html));

  await withDeadline('fixture HTTP startup', fixtureDeadlines.shutdownMs,
    () => app.listen({host: '127.0.0.1', port}), cleanupStartup);
  startupComplete = true;
  const address = app.server.address() as AddressInfo;
  let closed = false;
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    finalOutputPath,
    close: async () => {
      if (closed) return;
      closed = true;
      let closeFailure: unknown;
      try {
        await withDeadline('fixture queue drain', fixtureDeadlines.queueDrainMs, () => queue.close(), async () => queue.abort());
      } catch (error) {
        closeFailure = error;
      }
      try {
        app.server.closeAllConnections();
        await withDeadline('fixture HTTP shutdown', fixtureDeadlines.shutdownMs, () => app.close(), async () => {
          app.server.closeAllConnections();
        });
      } catch (error) {
        closeFailure ??= error;
      }
      try {
        await rm(runDirectory, {recursive: true, force: true});
      } catch (error) {
        closeFailure ??= error;
      }
      if (closeFailure) throw closeFailure;
    }
  };
  } finally {
    if (!startupComplete) {
      await cleanupStartup();
      await rm(runDirectory, {recursive: true, force: true});
    }
  }
};

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

const main = async (): Promise<void> => {
  const requestedPort = Number(process.env.HUANGQUE_PORT ?? '4173');
  if (!Number.isInteger(requestedPort) || requestedPort < 1 || requestedPort > 65_535) {
    throw new Error('HUANGQUE_PORT must be an integer from 1 to 65535');
  }
  const composition = await startLocalComposition({port: requestedPort});
  process.stdout.write(`Huangque fixture composition listening at ${composition.baseUrl}\n`);
  await new Promise<void>((resolveShutdown) => {
    let stopping = false;
    const stop = () => {
      if (stopping) return;
      stopping = true;
      void composition.close().then(resolveShutdown, (error) => {
        process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
        process.exitCode = 1;
        resolveShutdown();
      });
    };
    process.once('SIGINT', stop);
    process.once('SIGTERM', stop);
  });
};

if (isMain) void main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
