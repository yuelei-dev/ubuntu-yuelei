import {randomUUID} from 'node:crypto';
import {readFile, mkdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {Queue} from 'bullmq';
import {Client as MinioClient} from 'minio';
import {buildStoryboard} from '@huangque/director';
import {inspectOutput} from '@huangque/media';
import {MockAvatarProvider, MockImageProvider} from '@huangque/providers';
import {createPostgresProjectRepository} from '../apps/api/src/db/drizzle-project-repository.js';
import {createBullMqQueueAdapter} from '../apps/api/src/queue.js';
import {createBullMqPipelineWorker} from '../apps/worker/src/bullmq-worker.js';
import {Task4PipelineRepository} from '../apps/worker/src/pipeline.js';
import {withDeadline} from './deadline.js';
import {parseDockerRuntimeConfig} from './docker-config.js';
import {fixtureDeadlines, prepareFixtureBundles, renderFixtureVideo, resolveChromeExecutable} from './start-local-composition.js';
import {assertFixtureVideoVisuals, fixtureVisualSampleTimes} from './video-visual-analysis.js';
import {RedisProjectEventBroker} from './redis-project-events.js';

const queueName = 'huangque-project-jobs';

export const runDockerWorker = async (environment: NodeJS.ProcessEnv = process.env): Promise<void> => {
  const config = parseDockerRuntimeConfig(environment, 'worker');
  const minioConfig = config.minio!;
  const runDirectory = resolve(tmpdir(), 'huangque-video-workbench', 'docker-worker', randomUUID());
  await mkdir(runDirectory, {recursive: true});
  const controller = new AbortController();
  const {repository, pool} = createPostgresProjectRepository(config.databaseUrl);
  const queue = new Queue(queueName, {connection: config.redis});
  const queueAdapter = createBullMqQueueAdapter(queue);
  const projectEvents = new RedisProjectEventBroker(config.redis);
  const pipelineRepository = new Task4PipelineRepository(repository, () => new Date(), projectEvents);
  const minio = new MinioClient(minioConfig);
  const chromeExecutable = resolveChromeExecutable();
  let worker: ReturnType<typeof createBullMqPipelineWorker> | undefined;
  try {
  await withDeadline('Docker worker Redis event startup', 20_000, () => projectEvents.ready(), () => projectEvents.close());
  const {serveUrl} = await withDeadline('Docker worker bundle preparation', fixtureDeadlines.bundleMs, (signal) =>
    prepareFixtureBundles(runDirectory, signal));

  await withDeadline('MinIO bucket startup', 20_000, async () => {
    if (!await minio.bucketExists(minioConfig.bucket)) await minio.makeBucket(minioConfig.bucket);
    await minio.setBucketPolicy(minioConfig.bucket, JSON.stringify({
      Version: '2012-10-17',
      Statement: [{Effect: 'Allow', Principal: {AWS: ['*']}, Action: ['s3:GetObject'], Resource: [`arn:aws:s3:::${minioConfig.bucket}/*`]}]
    }));
  });

  const createdWorker = createBullMqPipelineWorker({
    queueName,
    connection: config.redis,
    repository: pipelineRepository,
    queue: queueAdapter,
    buildStoryboard,
    avatarProvider: new MockAvatarProvider(),
    imageProvider: new MockImageProvider(),
    qualityInspector: {
      async inspect(project) {
        const scenes = await pipelineRepository.listScenes(project.id);
        const rendered = await renderFixtureVideo({
          projectId: project.id, scenes, runDirectory, chromeExecutable, serveUrl, signal: controller.signal
        });
        const reportPath = resolve(runDirectory, project.id, 'quality.json');
        const report = await inspectOutput(rendered.outputPath, {
          width: 1080, height: 1920, durationMs: rendered.durationMs, durationToleranceMs: 100, expectedFrameRate: 30
        }, {reportPath});
        await assertFixtureVideoVisuals({videoPath: rendered.outputPath, ...fixtureVisualSampleTimes(scenes)});
        const videoKey = `projects/${project.id}/preview.mp4`;
        const reportKey = `projects/${project.id}/quality.json`;
        const video = await readFile(rendered.outputPath);
        const quality = await readFile(reportPath);
        await minio.putObject(minioConfig.bucket, videoKey, video, video.length, {'Content-Type': 'video/mp4'});
        await minio.putObject(minioConfig.bucket, reportKey, quality, quality.length, {'Content-Type': 'application/json'});
        const previewUrl = `${minioConfig.publicEndpoint}/${minioConfig.bucket}/${videoKey}`;
        return {report, reportPath: `s3://${minioConfig.bucket}/${reportKey}`, previewUrl, downloadUrl: previewUrl};
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
