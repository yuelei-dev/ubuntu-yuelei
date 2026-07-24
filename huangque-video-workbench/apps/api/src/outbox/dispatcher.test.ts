import {describe, expect, it, vi} from 'vitest';
import {InMemoryProjectRepository, type JobRecord} from '../services/project-service.js';
import {InMemoryQueue, type QueueAdapter} from '../queue.js';
import {OutboxDispatcher} from './dispatcher.js';

const pending = (id: string, now: Date): JobRecord => ({
  id, projectId: 'project-1', sceneId: 'project', taskType: 'storyboard.generate',
  inputHash: 'hash', dispatchPayload: {script: id}, status: 'PENDING', createdAt: now.toISOString()
});

describe('OutboxDispatcher', () => {
  it('recovers a committed job after a queue outage and deduplicates delivery', async () => {
    let now = new Date('2026-01-01T00:00:00Z');
    const repository = new InMemoryProjectRepository(() => now);
    await repository.reserveJob(pending('job-1', now));
    let available = false;
    const delivered = new InMemoryQueue();
    const queue: QueueAdapter = {submit: async (job) => {
      if (!available) throw new Error('redis unavailable');
      return delivered.submit(job);
    }};
    const dispatcher = new OutboxDispatcher(repository, queue, {owner: 'one', now: () => now, baseBackoffMs: 100});

    expect(await dispatcher.dispatchOnce()).toBe(0);
    expect((await repository.findJob('job-1'))?.dispatchAttempts).toBe(1);
    available = true;
    now = new Date(now.getTime() + 100);
    expect(await dispatcher.dispatchOnce()).toBe(1);
    expect(await dispatcher.dispatchOnce()).toBe(0);
    expect(delivered.jobs()).toHaveLength(1);
    expect(delivered.jobs()[0]).toMatchObject({id: 'job-1', data: {input: {script: 'job-1'}}});
  });

  it('allows only one concurrent dispatcher to claim a due row', async () => {
    const now = new Date('2026-01-01T00:00:00Z');
    const repository = new InMemoryProjectRepository(() => now);
    await repository.reserveJob(pending('job-1', now));
    const queue = new InMemoryQueue();
    const left = new OutboxDispatcher(repository, queue, {owner: 'left', now: () => now});
    const right = new OutboxDispatcher(repository, queue, {owner: 'right', now: () => now});
    expect((await Promise.all([left.dispatchOnce(), right.dispatchOnce()])).reduce((a, b) => a + b)).toBe(1);
    expect(queue.jobs()).toHaveLength(1);
  });

  it('never claims legacy pending rows with undefined or null immutable payloads', async () => {
    const now = new Date('2026-01-01T00:00:00Z');
    const repository = new InMemoryProjectRepository(() => now);
    const legacy = pending('legacy-job', now);
    delete legacy.dispatchPayload;
    await repository.reserveJob(legacy);
    await repository.reserveJob({...pending('legacy-null-job', now), dispatchPayload: null});
    const queue = new InMemoryQueue();
    const dispatcher = new OutboxDispatcher(repository, queue, {owner: 'one', now: () => now});
    expect(await dispatcher.dispatchOnce()).toBe(0);
    expect(queue.jobs()).toEqual([]);
    expect((await repository.findJob(legacy.id))?.status).toBe('PENDING');
    expect((await repository.findJob('legacy-null-job'))?.status).toBe('PENDING');
  });

  it('reclaims an abandoned lease after it expires', async () => {
    let now = new Date('2026-01-01T00:00:00Z');
    const repository = new InMemoryProjectRepository(() => now);
    await repository.reserveJob(pending('job-1', now));
    expect(await repository.claimDueJobs('crashed', 1, 50)).toHaveLength(1);
    const dispatcher = new OutboxDispatcher(repository, new InMemoryQueue(), {owner: 'replacement', now: () => now});
    expect(await dispatcher.dispatchOnce()).toBe(0);
    now = new Date(now.getTime() + 51);
    expect(await dispatcher.dispatchOnce()).toBe(1);
  });

  it('uses capped exponential backoff', async () => {
    let now = new Date('2026-01-01T00:00:00Z');
    const repository = new InMemoryProjectRepository(() => now);
    await repository.reserveJob(pending('job-1', now));
    const dispatcher = new OutboxDispatcher(repository, {submit: async () => { throw new Error('down'); }}, {
      owner: 'one', now: () => now, baseBackoffMs: 10, maxBackoffMs: 25
    });
    for (const expected of [10, 20, 25, 25]) {
      await dispatcher.dispatchOnce();
      const job = await repository.findJob('job-1');
      expect(new Date(job!.nextDispatchAt!).getTime() - now.getTime()).toBe(expected);
      now = new Date(job!.nextDispatchAt!);
    }
  });

  it('stops polling and returns within the shutdown deadline', async () => {
    vi.useFakeTimers();
    try {
      const now = new Date('2026-01-01T00:00:00Z');
      const repository = new InMemoryProjectRepository(() => now);
      await repository.reserveJob(pending('job-1', now));
      const dispatcher = new OutboxDispatcher(repository, {submit: () => new Promise(() => undefined)}, {pollIntervalMs: 5});
      dispatcher.start();
      const closed = dispatcher.close(20);
      await vi.advanceTimersByTimeAsync(20);
      await expect(closed).resolves.toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });
});
