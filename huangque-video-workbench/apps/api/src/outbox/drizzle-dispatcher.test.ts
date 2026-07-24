import {readFile, readdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {PGlite} from '@electric-sql/pglite';
import {drizzle} from 'drizzle-orm/pglite';
import {afterEach, describe, expect, it} from 'vitest';
import {DrizzleProjectRepository} from '../db/drizzle-project-repository.js';
import {InMemoryQueue} from '../queue.js';
import type {JobRecord} from '../services/project-service.js';
import {OutboxDispatcher} from './dispatcher.js';

const migrationDirectory = resolve(process.cwd(), 'apps', 'api', 'drizzle');
const databases: PGlite[] = [];

const createRepository = async () => {
  const database = new PGlite();
  databases.push(database);
  for (const file of (await readdir(migrationDirectory)).filter((candidate) => candidate.endsWith('.sql')).sort()) {
    const migration = await readFile(resolve(migrationDirectory, file), 'utf8');
    for (const statement of migration.split('--> statement-breakpoint')) {
      if (statement.trim()) await database.exec(statement);
    }
  }
  const repository = new DrizzleProjectRepository(drizzle(database) as never);
  const now = new Date('2026-01-01T00:00:00Z').toISOString();
  await repository.createProject({
    id: 'project-1', ownerUsername: 'alice', title: 'Project', status: 'COMPLETED',
    input: {type: 'script', content: 'script'}, avatar: {avatarId: 'a', voiceId: 'v'},
    output: {templateId: 't'}, createdAt: now, updatedAt: now
  });
  await repository.createScene({
    id: 'scene-1', projectId: 'project-1', order: 1, status: 'READY', script: 'scene',
    visual: {sceneType: 'image', purpose: 'visual', durationEstimate: 2, layout: 'visual_full', highlightWords: [], contentRevision: 0},
    createdAt: now, updatedAt: now
  });
  return {database, repository};
};

const generationJob = (id: string): JobRecord => ({
  id, projectId: 'project-1', sceneId: 'scene-1', taskType: 'scene.asset.generate',
  inputHash: id, dispatchPayload: {projectId: 'project-1', sceneId: 'scene-1'},
  status: 'PENDING', options: {attempts: 3, backoff: {type: 'exponential', delay: 100}, contentRevision: 0},
  createdAt: new Date('2026-01-01T00:00:00Z').toISOString()
});

afterEach(async () => {
  await Promise.all(databases.splice(0).map((database) => database.close()));
});

describe('Drizzle outbox transaction boundary', () => {
  it('rolls back a generation reservation when canonical activation conflicts', async () => {
    const {repository} = await createRepository();
    const job = generationJob('conflicting-job');
    expect(await repository.reserveAndActivateSceneRegeneration(job, 'different-active-job', 0, 'GENERATING_ASSETS'))
      .toBeUndefined();
    expect(await repository.findJob(job.id)).toBeUndefined();
    expect((await repository.findProjectForWorker('project-1'))?.scenes[0]?.visual.activeGenerationJobId)
      .toBeUndefined();
  });

  it('exposes the generation job and active scene identity together, then claims through the real dispatcher path', async () => {
    const {repository} = await createRepository();
    const job = generationJob('committed-job');
    const result = await repository.reserveAndActivateSceneRegeneration(job, undefined, 0, 'GENERATING_ASSETS');
    expect(result?.job.id).toBe(job.id);
    const committed = await repository.findProjectForWorker('project-1');
    expect(committed?.scenes[0]?.visual.activeGenerationJobId).toBe(job.id);
    const queue = new InMemoryQueue();
    const dispatcher = new OutboxDispatcher(repository, queue, {owner: 'dispatcher'});
    expect(await dispatcher.dispatchOnce()).toBe(1);
    expect(queue.jobs()).toHaveLength(1);
    expect((await repository.findJob(job.id))?.status).toBe('QUEUED');
  });

});
