import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {mkdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {createProductionApp} from '../apps/api/src/production.js';
import {withDeadline} from './deadline.js';
import {parseDockerRuntimeConfig} from './docker-config.js';
import {fixtureDeadlines, prepareFixtureBundles} from './start-local-composition.js';
import {RedisProjectEventBroker} from './redis-project-events.js';

export const runDockerApi = async (environment: NodeJS.ProcessEnv = process.env): Promise<void> => {
  const config = parseDockerRuntimeConfig(environment, 'api');
  const runDirectory = resolve(tmpdir(), 'huangque-video-workbench', 'docker-api', randomUUID());
  await mkdir(runDirectory, {recursive: true});
  const projectEvents = new RedisProjectEventBroker(config.redis);
  const production = createProductionApp({databaseUrl: config.databaseUrl, redisConnection: config.redis, projectEvents});
  try {
  await withDeadline('Docker API Redis event startup', 20_000, () => projectEvents.ready(), () => projectEvents.close());
  const {clientBundle} = await withDeadline('Docker API client bundle', fixtureDeadlines.bundleMs,
    (signal) => prepareFixtureBundles(runDirectory, signal));
  const html = '<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Huangque Video Workbench</title></head><body><div id="root"></div><script type="module" src="/fixture-client.js"></script></body></html>';
  production.app.get('/healthz', async () => ({status: 'ok'}));
  production.app.get('/fixture-client.js', async (_request, reply) => reply.type('text/javascript; charset=utf-8').send(clientBundle));
  production.app.get('/', async (_request, reply) => reply.type('text/html; charset=utf-8').send(html));
  production.app.get('/projects/new', async (_request, reply) => reply.type('text/html; charset=utf-8').send(html));
  production.app.get<{Params: {id: string}}>('/projects/:id', async (_request, reply) => reply.type('text/html; charset=utf-8').send(html));
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
