import {randomUUID} from 'node:crypto';
import {readFile, mkdir, open as openFile, rm, stat} from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import {Readable} from 'node:stream';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {Queue} from 'bullmq';
import {Client as MinioClient} from 'minio';
import {buildStoryboard} from '@huangque/director';
import {inspectOutput} from '@huangque/media';
import {assertPublicHttpsUrl, createSsrfSafeFetch, HttpAvatarProvider, HttpImageProvider, providerInputHash, validatePublicDns} from '@huangque/providers';
import {createPostgresProjectRepository} from '../apps/api/src/db/drizzle-project-repository.js';
import {createBullMqQueueAdapter} from '../apps/api/src/queue.js';
import {createBullMqPipelineWorker} from '../apps/worker/src/bullmq-worker.js';
import {Task4PipelineRepository} from '../apps/worker/src/pipeline.js';
import {withDeadline} from './deadline.js';
import {parseDockerRuntimeConfig} from './docker-config.js';
import {RedisProjectEventBroker} from './redis-project-events.js';
import {z} from 'zod';

const queueName = 'huangque-project-jobs';

type BucketClient = {
  bucketExists(bucket: string): Promise<boolean>;
  makeBucket(bucket: string): Promise<void>;
  removeBucketPolicy(bucket: string): Promise<void>;
};

type UploadData = Buffer | Readable;
type ObjectUploader = {
  putObject(bucket: string, key: string, data: UploadData, size: number, metadata: Record<string, string>): Promise<unknown>;
};
type AttemptObjectClient = ObjectUploader & {removeObject(bucket: string, key: string): Promise<unknown>};

export const renderAttemptObjectKeys = (projectId: string, attemptId: string): {preview: string; report: string} => ({
  preview: `projects/${projectId}/attempts/${attemptId}/preview.mp4`,
  report: `projects/${projectId}/attempts/${attemptId}/quality.json`
});

export const putObjectWithinDeadline = async (
  client: ObjectUploader, bucket: string, key: string, data: UploadData, size: number,
  metadata: Record<string, string>, signal: AbortSignal, timeoutMs: number
): Promise<void> => {
  await withDeadline(`MinIO upload ${key}`, timeoutMs, async (deadlineSignal) => {
    const combined = AbortSignal.any([signal, deadlineSignal]);
    combined.throwIfAborted();
    const abortUpload = () => {
      if (!Buffer.isBuffer(data)) data.destroy(combined.reason instanceof Error ? combined.reason : new Error('upload aborted'));
    };
    combined.addEventListener('abort', abortUpload, {once: true});
    const upload = client.putObject(bucket, key, data, size, metadata);
    try {
      await Promise.race([
        upload,
        new Promise<never>((_resolve, reject) => combined.addEventListener('abort', () => reject(combined.reason), {once: true}))
      ]);
      combined.throwIfAborted();
    } finally {
      combined.removeEventListener('abort', abortUpload);
      void upload.catch(() => undefined);
    }
  }, async () => {
    if (!Buffer.isBuffer(data)) data.destroy(new Error('MinIO upload deadline exceeded'));
  });
};

export const uploadAttemptObjects = async (options: {
  client: AttemptObjectClient; bucket: string; videoKey: string; reportKey: string;
  video: Readable; videoSize: number; report: Buffer; signal: AbortSignal; timeoutMs: number;
}): Promise<void> => {
  try {
    await putObjectWithinDeadline(
      options.client, options.bucket, options.videoKey, options.video, options.videoSize,
      {'Content-Type': 'video/mp4'}, options.signal, options.timeoutMs
    );
    await putObjectWithinDeadline(
      options.client, options.bucket, options.reportKey, Readable.from(options.report), options.report.length,
      {'Content-Type': 'application/json'}, options.signal, options.timeoutMs
    );
  } catch (error) {
    const cleanupDeadlineMs = Math.min(options.timeoutMs, 10_000);
    const cleanup = await Promise.allSettled([options.videoKey, options.reportKey].map((key) =>
      withDeadline(`MinIO failed-attempt cleanup ${key}`, cleanupDeadlineMs, async () => {
        await options.client.removeObject(options.bucket, key);
      })
    ));
    const cleanupErrors = cleanup
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map((result) => result.reason);
    if (cleanupErrors.length > 0 && error instanceof Error) {
      Object.defineProperty(error, 'cleanupErrors', {value: cleanupErrors, enumerable: false});
    }
    throw error;
  }
};

const safeProviderUrl = (raw: string, label: string): URL => {
  return assertPublicHttpsUrl(raw, label);
};

const RenderProviderResponseSchema = z.object({
  outputUrl: z.string().url(),
  inputHash: z.string().regex(/^[a-f0-9]{64}$/),
  provenance: z.enum(['generated', 'enterprise'])
}).strict();
export const MAX_RENDER_OUTPUT_BYTES = 512 * 1024 * 1024;

export const requestProductionRender = async (options: {
  endpoint: string; token: string; timeoutMs: number; allowedMediaOrigins: string[];
  input: unknown; outputPath: string; signal: AbortSignal; fetch?: typeof fetch; maxOutputBytes?: number;
}): Promise<void> => {
  const endpoint = safeProviderUrl(options.endpoint, 'render provider endpoint');
  const inputHash = providerInputHash(options.input);
  const signal = AbortSignal.any([options.signal, AbortSignal.timeout(options.timeoutMs)]);
  const fetchImpl = options.fetch ?? createSsrfSafeFetch();
  const maxOutputBytes = options.maxOutputBytes ?? MAX_RENDER_OUTPUT_BYTES;
  if (!Number.isInteger(maxOutputBytes) || maxOutputBytes < 1 || maxOutputBytes > MAX_RENDER_OUTPUT_BYTES) {
    throw new Error('render output byte limit is invalid');
  }
  const response = await fetchImpl(endpoint, {
    method: 'POST', redirect: 'error', signal,
    headers: {'authorization': `Bearer ${options.token}`, 'content-type': 'application/json'},
    body: JSON.stringify({...options.input as object, inputHash})
  });
  if (!response.ok) throw new Error(`render provider request failed with HTTP ${response.status}`);
  const value = RenderProviderResponseSchema.parse(await response.json());
  if (value.inputHash !== inputHash) throw new Error('render provider returned an unbound response');
  const outputUrl = safeProviderUrl(value.outputUrl, 'render provider output');
  const allowedOrigins = new Set(options.allowedMediaOrigins.map((origin) => safeProviderUrl(origin, 'allowed media origin').origin));
  if (!allowedOrigins.has(outputUrl.origin)) throw new Error('render output origin is not explicitly allowed');
  const output = await fetchImpl(outputUrl, {
    headers: {'authorization': `Bearer ${options.token}`}, redirect: 'error', signal
  });
  if (!output.ok) throw new Error(`render output download failed with HTTP ${output.status}`);
  const length = Number(output.headers.get('content-length') ?? 0);
  if (length > maxOutputBytes) throw new Error('render output exceeds its byte limit');
  if (!output.body) throw new Error('render output response has no body');
  const reader = output.body.getReader();
  const abortReader = () => { void reader.cancel(signal.reason).catch(() => undefined); };
  signal.addEventListener('abort', abortReader, {once: true});
  const file = await openFile(options.outputPath, 'wx');
  let received = 0;
  try {
    while (true) {
      signal.throwIfAborted();
      const chunk = await reader.read();
      if (chunk.done) break;
      received += chunk.value.byteLength;
      if (received > maxOutputBytes) {
        await reader.cancel('render output exceeds its byte limit');
        throw new Error('render output exceeds its byte limit');
      }
      await file.write(chunk.value);
    }
    if (received === 0) throw new Error('render output has an invalid size');
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    await file.close().catch(() => undefined);
    await rm(options.outputPath, {force: true}).catch(() => undefined);
    throw error;
  } finally {
    signal.removeEventListener('abort', abortReader);
  }
  await file.close();
};

const isMissingBucketPolicy = (error: unknown): boolean => typeof error === 'object' && error !== null &&
  'code' in error && ['NoSuchBucketPolicy', 'NoSuchKey', 'NoSuchBucket'].includes(String((error as {code: unknown}).code));

/** Removes any historical anonymous policy; absent policies are already private. */
export const ensurePrivateBucket = async (minio: BucketClient, bucket: string): Promise<void> => {
  if (!await minio.bucketExists(bucket)) await minio.makeBucket(bucket);
  try {
    await minio.removeBucketPolicy(bucket);
  } catch (error) {
    if (!isMissingBucketPolicy(error)) throw error;
  }
};

const privateBucketClient = (minio: MinioClient): BucketClient => ({
  bucketExists: (bucket) => minio.bucketExists(bucket),
  makeBucket: (bucket) => minio.makeBucket(bucket),
  // MinIO SDK 8 removes a policy through setBucketPolicy(bucket, '').
  removeBucketPolicy: (bucket) => minio.setBucketPolicy(bucket, '')
});

export const runDockerWorker = async (environment: NodeJS.ProcessEnv = process.env): Promise<void> => {
  const config = parseDockerRuntimeConfig(environment, 'worker', environment.HUANGQUE_RUNTIME_MODE === 'development' ? 'development' : 'production');
  await validatePublicDns([
    config.providers.avatarEndpoint,
    config.providers.imageEndpoint,
    config.providers.renderEndpoint,
    ...config.providers.allowedMediaOrigins
  ]);
  const minioConfig = config.minio;
  const runDirectory = resolve(tmpdir(), 'huangque-video-workbench', 'docker-worker', randomUUID());
  await mkdir(runDirectory, {recursive: true});
  const controller = new AbortController();
  const {repository, pool} = createPostgresProjectRepository(config.databaseUrl);
  const queue = new Queue(queueName, {connection: config.redis});
  const queueAdapter = createBullMqQueueAdapter(queue);
  const projectEvents = new RedisProjectEventBroker(config.redis);
  const pipelineRepository = new Task4PipelineRepository(repository, () => new Date(), projectEvents);
  const minio = new MinioClient(minioConfig);
  let worker: ReturnType<typeof createBullMqPipelineWorker> | undefined;
  try {
  await withDeadline('Docker worker Redis event startup', 20_000, () => projectEvents.ready(), () => projectEvents.close());
  await withDeadline('MinIO bucket startup', 20_000, () => ensurePrivateBucket(privateBucketClient(minio), minioConfig.bucket));

  const createdWorker = createBullMqPipelineWorker({
    queueName,
    connection: config.redis,
    repository: pipelineRepository,
    queue: queueAdapter,
    buildStoryboard,
    avatarProvider: new HttpAvatarProvider({
      endpoint: config.providers.avatarEndpoint, token: config.providers.token, timeoutMs: config.providers.timeoutMs,
      allowedMediaOrigins: config.providers.allowedMediaOrigins
    }),
    imageProvider: new HttpImageProvider({
      endpoint: config.providers.imageEndpoint, token: config.providers.token, timeoutMs: config.providers.timeoutMs,
      allowedMediaOrigins: config.providers.allowedMediaOrigins
    }),
    qualityInspector: {
      async inspect(project, signal, attempt) {
        const scenes = await pipelineRepository.listScenes(project.id);
        const renderSignal = signal ? AbortSignal.any([controller.signal, signal]) : controller.signal;
        const attemptId = attempt?.owner ?? randomUUID();
        const attemptDirectory = resolve(runDirectory, project.id, attemptId);
        await mkdir(attemptDirectory, {recursive: true});
        const outputPath = resolve(attemptDirectory, 'preview.mp4');
        const renderInput = {
          projectId: project.id, attemptId, title: project.title, script: project.script,
          templateId: project.output.templateId, scenes
        };
        await requestProductionRender({
          endpoint: config.providers.renderEndpoint, token: config.providers.token,
          timeoutMs: config.providers.timeoutMs, allowedMediaOrigins: config.providers.allowedMediaOrigins,
          input: renderInput, outputPath, signal: renderSignal
        });
        const reportPath = resolve(runDirectory, project.id, attemptId, 'quality.json');
        const durationMs = scenes.reduce((sum, scene) => sum + scene.durationEstimate * 1000, 0);
        const report = await inspectOutput(outputPath, {
          width: 1080, height: 1920, durationMs, durationToleranceMs: 250, expectedFrameRate: 30
        }, {reportPath, signal: renderSignal});
        const {preview: videoKey, report: reportKey} = renderAttemptObjectKeys(project.id, attemptId);
        const videoSize = (await stat(outputPath)).size;
        if (videoSize < 1 || videoSize > 256 * 1024 * 1024) throw new Error('rendered output exceeds the 256 MiB upload limit');
        const quality = await readFile(reportPath);
        renderSignal.throwIfAborted();
        await uploadAttemptObjects({
          client: minio, bucket: minioConfig.bucket, videoKey, reportKey,
          video: createReadStream(outputPath), videoSize, report: quality,
          signal: renderSignal, timeoutMs: 30_000
        });
        return {report, reportPath: `s3://${minioConfig.bucket}/${reportKey}`, previewUrl: videoKey, downloadUrl: videoKey};
      }
    }
  });
  worker = createdWorker;
  await withDeadline('Docker worker startup', 30_000, () => createdWorker.waitUntilReady(), async () => { await createdWorker.close(true); });
  process.stdout.write(`Huangque worker connected to Redis and MinIO bucket ${minioConfig.bucket}\n`);

  await new Promise<void>((resolveShutdown) => {
    const stop = () => {
      resolveShutdown();
    };
    process.once('SIGINT', stop);
    process.once('SIGTERM', stop);
  });
  } finally {
    controller.abort(new Error('Docker worker shutdown'));
    await withDeadline('Docker worker shutdown', 15_000, async () => {
      await Promise.allSettled([worker?.close(true), queue.close(), projectEvents.close(), pool.end()]);
      await rm(runDirectory, {recursive: true, force: true});
    });
  }
};

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isMain) void runDockerWorker().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
