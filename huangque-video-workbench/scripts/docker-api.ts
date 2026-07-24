import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {mkdir, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {createProductionApp} from '../apps/api/src/production.js';
import {withDeadline} from './deadline.js';
import {parseDockerRuntimeConfig} from './docker-config.js';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {RedisProjectEventBroker} from './redis-project-events.js';
import {Client as MinioClient} from 'minio';
import type {FastifyInstance} from 'fastify';
import {PassThrough, type Readable} from 'node:stream';
import {ObjectReadTimeoutError} from '../apps/api/src/routes/output.js';

const developmentSessionCookie = 'hq_session=localdev; Path=/; HttpOnly; SameSite=Lax';
const execFileAsync = promisify(execFile);

export const prepareProductionWebBundle = async (runDirectory: string, signal: AbortSignal): Promise<string> => {
  const repositoryRoot = resolve(import.meta.dirname, '..');
  const cli = resolve(repositoryRoot, 'node_modules', 'tsx', 'dist', 'cli.mjs');
  const script = resolve(repositoryRoot, 'scripts', 'prepare-web-bundle.ts');
  const {stdout} = await execFileAsync(process.execPath, [cli, script, repositoryRoot, runDirectory], {
    cwd: repositoryRoot, encoding: 'utf8', maxBuffer: 1024 * 1024, signal, windowsHide: true
  });
  const result = JSON.parse(stdout.trim().split(/\r?\n/u).at(-1) ?? '{}') as {clientBundlePath?: unknown};
  if (typeof result.clientBundlePath !== 'string') throw new Error('web bundle process returned no bundle path');
  return readFile(result.clientBundlePath, 'utf8');
};

export const openMinioObjectWithinDeadline = async (
  open: () => Promise<Readable>,
  timeoutMs = 10_000,
  idleTimeoutMs = 15_000
): Promise<Readable> => {
  let stream: Readable | undefined;
  try {
    const upstream = await withDeadline('MinIO output first byte', timeoutMs, async (signal) => {
      stream = await open();
      if (stream.readableLength > 0) return stream;
      await new Promise<void>((resolveReady, reject) => {
        const cleanup = () => {
          stream?.off('readable', ready);
          stream?.off('error', failed);
          stream?.off('end', ended);
          signal.removeEventListener('abort', aborted);
        };
        const ready = () => { cleanup(); resolveReady(); };
        const failed = (error: Error) => { cleanup(); reject(error); };
        const ended = () => { cleanup(); reject(new Error('MinIO output ended before its first byte')); };
        const aborted = () => { cleanup(); reject(signal.reason); };
        stream!.once('readable', ready);
        stream!.once('error', failed);
        stream!.once('end', ended);
        signal.addEventListener('abort', aborted, {once: true});
      });
      return stream;
    }, async () => { stream?.destroy(); });
    const downstream = new PassThrough();
    let idle: NodeJS.Timeout;
    const expire = () => {
      const error = new ObjectReadTimeoutError('object storage output became idle');
      upstream.destroy(error);
    };
    const reset = () => {
      clearTimeout(idle);
      idle = setTimeout(expire, idleTimeoutMs);
      idle.unref();
    };
    const clear = () => clearTimeout(idle);
    upstream.on('data', reset);
    upstream.once('end', clear);
    upstream.once('error', (error) => downstream.destroy(error));
    upstream.once('close', clear);
    downstream.once('close', () => {
      clear();
      if (!upstream.destroyed) upstream.destroy();
    });
    reset();
    upstream.pipe(downstream);
    return downstream;
  } catch (error) {
    if (error instanceof Error && error.name === 'FixtureTimeoutError') {
      throw new ObjectReadTimeoutError('object storage did not deliver its first byte in time');
    }
    throw error;
  }
};

export const browserShell = ({uiBasePath, apiBasePath}: {uiBasePath: string; apiBasePath: string}): string =>
  `<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
  `<meta name="workbench-ui-base" content="${uiBasePath}"><meta name="workbench-api-base" content="${apiBasePath}">` +
  `<title>Huangque Video Workbench</title></head><body><div id="root"></div>` +
  `<script type="module" src="${uiBasePath}/workbench-client.js"></script></body></html>`;

export const registerDockerBrowserRoutes = (
  app: FastifyInstance, {clientBundle, html, developmentMode}: {clientBundle: string; html: string; developmentMode: boolean}
): void => {
  const shell = async (_request: unknown, reply: {header(name: string, value: string): unknown; type(value: string): {send(value: string): unknown}}) => {
    if (developmentMode) reply.header('set-cookie', developmentSessionCookie);
    return reply.type('text/html; charset=utf-8').send(html);
  };
  app.get('/workbench-client.js', async (_request, reply) => reply.type('text/javascript; charset=utf-8').send(clientBundle));
  app.get('/', shell);
  app.get('/projects/new', shell);
  app.get('/projects/:id', shell);
};

export const runDockerApi = async (environment: NodeJS.ProcessEnv = process.env): Promise<void> => {
  const config = parseDockerRuntimeConfig(environment, 'api', environment.HUANGQUE_RUNTIME_MODE === 'development' ? 'development' : 'production');
  const runDirectory = resolve(tmpdir(), 'huangque-video-workbench', 'docker-api', randomUUID());
  await mkdir(runDirectory, {recursive: true});
  const projectEvents = new RedisProjectEventBroker(config.redis);
  const minio = new MinioClient(config.minio);
  const production = createProductionApp({
    databaseUrl: config.databaseUrl, redisConnection: config.redis, projectEvents, huangqueAuthBase: config.huangqueAuthBase,
    publicApiBasePath: environment.HUANGQUE_RUNTIME_MODE === 'development' ? '/api' : '/api/video-workbench',
    objectReader: {open: (objectKey) => openMinioObjectWithinDeadline(() => minio.getObject(config.minio.bucket, objectKey))}
  });
  try {
  await withDeadline('Docker API Redis event startup', 20_000, () => projectEvents.ready(), () => projectEvents.close());
  const clientBundle = await withDeadline('Docker API client bundle', 60_000,
    (signal) => prepareProductionWebBundle(runDirectory, signal));
  const productionMode = environment.HUANGQUE_RUNTIME_MODE !== 'development';
  const html = browserShell({
    uiBasePath: productionMode ? '/video-workbench' : '',
    apiBasePath: productionMode ? '/api/video-workbench' : '/api'
  });
  production.app.get('/healthz', async () => ({status: 'ok'}));
  production.app.get('/api/healthz', async () => ({status: 'ok'}));
  registerDockerBrowserRoutes(production.app, {
    clientBundle, html, developmentMode: environment.HUANGQUE_RUNTIME_MODE === 'development'
  });
  await withDeadline('Docker API startup', 30_000, () => production.app.listen({host: '0.0.0.0', port: config.port}), async () => {
    production.app.server.closeAllConnections();
    await production.close().catch(() => undefined);
  });
  process.stdout.write(`Huangque API listening on 0.0.0.0:${config.port}\n`);
  await new Promise<void>((resolveShutdown, rejectShutdown) => {
    const stop = () => {
      production.app.server.closeAllConnections();
      void withDeadline('Docker API shutdown', 10_000, async () => {
        await Promise.all([production.close(), projectEvents.close()]);
        await rm(runDirectory, {recursive: true, force: true});
      }, async () => { production.app.server.closeAllConnections(); }).then(resolveShutdown, rejectShutdown);
    };
    process.once('SIGINT', stop);
    process.once('SIGTERM', stop);
  });
  } catch (error) {
    production.app.server.closeAllConnections();
    await Promise.allSettled([production.close(), projectEvents.close(), rm(runDirectory, {recursive: true, force: true})]);
    throw error;
  }
};

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isMain) void runDockerApi().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
