import {randomUUID} from 'node:crypto';
import {Queue, type ConnectionOptions} from 'bullmq';
import {createApp} from './app.js';
import {createPostgresProjectRepository} from './db/drizzle-project-repository.js';
import {createBullMqQueueAdapter} from './queue.js';
import type {ProjectEventBroker} from './events.js';
import type {ObjectReader} from './routes/output.js';
import {authenticateHuangque} from './auth/huangque-auth.js';
import {OutboxDispatcher} from './outbox/dispatcher.js';

export type ProductionApiOptions = {
  databaseUrl: string;
  redisConnection: ConnectionOptions;
  projectEvents?: ProjectEventBroker;
  objectReader?: ObjectReader;
  huangqueAuthBase: string;
  publicApiBasePath?: string;
};

/**
 * Production composition only. It is never constructed by route tests, so
 * tests do not need PostgreSQL, Redis, Docker, or external credentials.
 */
export const createProductionApp = (options: ProductionApiOptions) => {
  const {repository, pool} = createPostgresProjectRepository(options.databaseUrl);
  const queue = new Queue('huangque-project-jobs', {connection: options.redisConnection});
  const queueAdapter = createBullMqQueueAdapter(queue);
  const dispatcher = new OutboxDispatcher(repository, queueAdapter);
  const app = createApp({
    repository, queue: queueAdapter, idFactory: randomUUID, projectEvents: options.projectEvents, objectReader: options.objectReader,
    publicApiBasePath: options.publicApiBasePath,
    authenticate: (cookie, signal) => authenticateHuangque(cookie, signal, options.huangqueAuthBase)
  });
  dispatcher.start();

  return {
    app,
    close: async (): Promise<void> => {
      await dispatcher.close(10_000);
      await Promise.all([app.close(), queue.close(), pool.end()]);
    }
  };
};
