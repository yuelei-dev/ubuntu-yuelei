import {randomUUID} from 'node:crypto';
import {Queue, type ConnectionOptions} from 'bullmq';
import {createApp} from './app.js';
import {createPostgresProjectRepository} from './db/drizzle-project-repository.js';
import {createBullMqQueueAdapter} from './queue.js';
import type {ProjectEventBroker} from './events.js';

export type ProductionApiOptions = {
  databaseUrl: string;
  redisConnection: ConnectionOptions;
  projectEvents?: ProjectEventBroker;
};

/**
 * Production composition only. It is never constructed by route tests, so
 * tests do not need PostgreSQL, Redis, Docker, or external credentials.
 */
export const createProductionApp = (options: ProductionApiOptions) => {
  const {repository, pool} = createPostgresProjectRepository(options.databaseUrl);
  const queue = new Queue('huangque-project-jobs', {connection: options.redisConnection});
  const app = createApp({repository, queue: createBullMqQueueAdapter(queue), idFactory: randomUUID, projectEvents: options.projectEvents});

  return {
    app,
    close: async (): Promise<void> => {
      await Promise.all([app.close(), queue.close(), pool.end()]);
    }
  };
};
