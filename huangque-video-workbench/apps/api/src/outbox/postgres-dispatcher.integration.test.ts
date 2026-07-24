import {readFile, readdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {Pool} from 'pg';
import {describe, expect, it} from 'vitest';
import {createPostgresProjectRepository} from '../db/drizzle-project-repository.js';
import {InMemoryQueue} from '../queue.js';
import type {JobRecord} from '../services/project-service.js';
import {OutboxDispatcher} from './dispatcher.js';

const databaseUrl = process.env.TEST_POSTGRES_DATABASE_URL;
const migrationDirectory = resolve(process.cwd(), 'apps', 'api', 'drizzle');

const withSearchPath = (connectionString: string, schema: string): string => {
  const url = new URL(connectionString);
  url.searchParams.set('options', `-c search_path=${schema} -c statement_timeout=2000`);
  url.searchParams.set('connect_timeout', '2');
  return url.toString();
};

const within = async <T>(operation: Promise<T>, label: string, deadlineMs = 2_000): Promise<T> => {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} exceeded ${deadlineMs}ms`)), deadlineMs);
      })
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
};

describe.skipIf(!databaseUrl)('PostgreSQL outbox isolation across independent connections', () => {
  it('returns zero before commit, then atomically observes and claims the activated job', async () => {
    const schema = `outbox_isolation_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    // max: 1 makes cleanup DROP queue behind any timed-out CREATE on the same
    // connection; statement_timeout guarantees that the earlier write settles.
    const admin = new Pool({
      connectionString: databaseUrl,
      max: 1,
      connectionTimeoutMillis: 2_000,
      statement_timeout: 2_000,
      query_timeout: 3_000
    });
    let writer: ReturnType<typeof createPostgresProjectRepository> | undefined;
    let observer: ReturnType<typeof createPostgresProjectRepository> | undefined;
    let release: (() => void) | undefined;
    let transaction: Promise<void> | undefined;
    let observerDispatch: Promise<number> | undefined;
    try {
      await within(admin.query(`create schema "${schema}"`), 'create test schema', 3_500);
      const migrated = await within(admin.connect(), 'connect migration client');
      try {
        await within(migrated.query(`set search_path to "${schema}"`), 'set migration search path', 3_500);
        for (const file of (await readdir(migrationDirectory)).filter((candidate) => candidate.endsWith('.sql')).sort()) {
          const migration = await readFile(resolve(migrationDirectory, file), 'utf8');
          for (const statement of migration.split('--> statement-breakpoint')) {
            if (statement.trim()) {
              await within(migrated.query(statement), `apply migration statement from ${file}`, 3_500);
            }
          }
        }
      } finally {
        migrated.release();
      }

      writer = createPostgresProjectRepository(withSearchPath(databaseUrl!, schema));
      observer = createPostgresProjectRepository(withSearchPath(databaseUrl!, schema));
      expect(writer.pool).not.toBe(observer.pool);
      const now = new Date().toISOString();
      await writer.repository.createProject({
        id: 'project-1', ownerUsername: 'alice', title: 'Project', status: 'COMPLETED',
        input: {type: 'script', content: 'script'}, avatar: {avatarId: 'a', voiceId: 'v'},
        output: {templateId: 't'}, createdAt: now, updatedAt: now
      });
      await writer.repository.createScene({
        id: 'scene-1', projectId: 'project-1', order: 1, status: 'READY', script: 'scene',
        visual: {sceneType: 'image', purpose: 'visual', durationEstimate: 2, layout: 'visual_full', highlightWords: [], contentRevision: 0},
        createdAt: now, updatedAt: now
      });
      const job: JobRecord = {
        id: 'generation-job', projectId: 'project-1', sceneId: 'scene-1', taskType: 'scene.asset.generate',
        inputHash: 'hash', dispatchPayload: {projectId: 'project-1', sceneId: 'scene-1'},
        status: 'PENDING', options: {attempts: 3, backoff: {type: 'exponential', delay: 100}, contentRevision: 0},
        createdAt: now
      };
      const gate = new Promise<void>((resolve) => { release = resolve; });
      let activated!: () => void;
      const activationReached = new Promise<void>((resolve) => { activated = resolve; });
      transaction = writer.repository.transaction(async () => {
        expect(await writer.repository.reserveAndActivateSceneRegeneration(job, undefined, 0, 'GENERATING_ASSETS')).toBeDefined();
        activated();
        await gate;
      });
      await within(Promise.race([
        activationReached,
        transaction.then(
          () => Promise.reject(new Error('writer transaction completed before reaching activation barrier')),
          (error) => Promise.reject(error)
        )
      ]), 'reach writer activation barrier', 3_500);

      const queue = new InMemoryQueue();
      const dispatcher = new OutboxDispatcher(observer.repository, queue, {owner: 'observer'});
      observerDispatch = dispatcher.dispatchOnce();
      let raceTimer: NodeJS.Timeout | undefined;
      let timelyResult: number | 'timeout';
      try {
        timelyResult = await Promise.race([
          observerDispatch,
          new Promise<'timeout'>((resolve) => {
            raceTimer = setTimeout(() => resolve('timeout'), 500);
          })
        ]);
      } finally {
        if (raceTimer) clearTimeout(raceTimer);
        release?.();
        await within(transaction.then(() => undefined, () => undefined), 'settle writer transaction');
        await within(observerDispatch.then(() => undefined, () => undefined), 'settle observer dispatch');
      }
      await transaction;
      transaction = undefined;
      observerDispatch = undefined;
      expect(timelyResult).toBe(0);
      expect(queue.jobs()).toEqual([]);

      const committed = await observer.repository.findProjectForWorker('project-1');
      expect(committed?.scenes[0]?.visual.activeGenerationJobId).toBe(job.id);
      expect((await observer.repository.findJob(job.id))?.status).toBe('PENDING');
      expect(await dispatcher.dispatchOnce()).toBe(1);
      expect(queue.jobs()).toHaveLength(1);
      expect((await observer.repository.findJob(job.id))?.status).toBe('QUEUED');
    } finally {
      const cleanupErrors: unknown[] = [];
      const cleanup = async (operation: Promise<unknown>): Promise<void> => {
        try {
          await operation;
        } catch (error) {
          cleanupErrors.push(error);
        }
      };
      release?.();
      if (transaction) await cleanup(within(transaction.then(() => undefined, () => undefined), 'cleanup writer transaction'));
      if (observerDispatch) await cleanup(within(observerDispatch.then(() => undefined, () => undefined), 'cleanup observer dispatch'));
      if (writer) await cleanup(within(writer.pool.end(), 'close writer pool'));
      if (observer) await cleanup(within(observer.pool.end(), 'close observer pool'));
      // Always attempt DROP. A timed-out CREATE may still have been in flight;
      // max:1 plus server-side statement_timeout orders this after it settles.
      await cleanup(within(admin.query(`drop schema if exists "${schema}" cascade`), 'drop test schema', 3_500));
      await cleanup(within(admin.end(), 'close admin pool'));
      if (cleanupErrors.length > 0) throw new AggregateError(cleanupErrors, 'PostgreSQL integration cleanup failed');
    }
  }, 20_000);
});
