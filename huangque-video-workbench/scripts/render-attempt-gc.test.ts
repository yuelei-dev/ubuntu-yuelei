import {describe, expect, it} from 'vitest';
import {cleanupKnownLosingAttempts, startAttemptRetentionJob} from './render-attempt-gc.js';

describe('render attempt orphan cleanup', () => {
  it('deletes only known losing local and MinIO artifacts within bounded best-effort calls', async () => {
    const local: string[] = [];
    const objects: string[] = [];
    await cleanupKnownLosingAttempts({
      winnerAttemptId: 'winner',
      losingAttempts: [
        {attemptId: 'loser-a', localPaths: ['runs/project/loser-a'], objectKeys: ['projects/p/attempts/loser-a/preview.mp4']},
        {attemptId: 'winner', localPaths: ['runs/project/winner'], objectKeys: ['projects/p/attempts/winner/preview.mp4']},
        {attemptId: 'loser-b', objectKeys: ['projects/p/attempts/loser-b/quality.json']}
      ],
      removeLocal: async (path) => { local.push(path); },
      removeObject: async (key) => { objects.push(key); },
      timeoutMs: 10
    });

    expect(local).toEqual(['runs/project/loser-a']);
    expect(objects).toEqual(['projects/p/attempts/loser-a/preview.mp4', 'projects/p/attempts/loser-b/quality.json']);
  });

  it('continues cleanup after a timed-out orphan delete', async () => {
    const objects: string[] = [];
    await cleanupKnownLosingAttempts({
      winnerAttemptId: 'winner',
      losingAttempts: [{attemptId: 'loser', objectKeys: ['stuck', 'next']}],
      removeObject: async (key) => {
        if (key === 'stuck') return await new Promise(() => undefined);
        objects.push(key);
      },
      timeoutMs: 10
    });
    expect(objects).toEqual(['next']);
  });

  it('runs a bounded retention sweep on startup and can be shut down', async () => {
    const objects: string[] = [];
    const job = startAttemptRetentionJob({
      intervalMs: 60_000,
      timeoutMs: 10,
      enumerate: async () => [{winnerAttemptId: 'winner', losingAttempts: [{attemptId: 'loser', objectKeys: ['orphan']}]}],
      removeObject: async (key) => { objects.push(key); }
    });
    await job.runNow();
    await job.close();
    expect(objects).toEqual(['orphan']);
  });
});
