import {rm} from 'node:fs/promises';
import {withDeadline} from './deadline.js';

export type KnownLosingAttempt = {
  attemptId: string;
  localPaths?: string[];
  objectKeys?: string[];
};

export type RenderAttemptCleanup = {
  winnerAttemptId: string;
  losingAttempts: KnownLosingAttempt[];
  timeoutMs: number;
  removeLocal?: (path: string) => Promise<void>;
  removeObject?: (key: string) => Promise<void>;
};

/** Removes only explicitly identified non-winner attempt artifacts. */
export const cleanupKnownLosingAttempts = async ({
  winnerAttemptId,
  losingAttempts,
  timeoutMs,
  removeLocal = async (path) => { await rm(path, {recursive: true, force: true}); },
  removeObject
}: RenderAttemptCleanup): Promise<void> => {
  const deletedLocal = new Set<string>();
  const deletedObjects = new Set<string>();
  const bestEffort = async (operation: string, work: () => Promise<void>): Promise<void> => {
    await withDeadline(operation, timeoutMs, async () => { await work(); }).catch(() => undefined);
  };
  for (const attempt of losingAttempts) {
    if (attempt.attemptId === winnerAttemptId) continue;
    for (const path of attempt.localPaths ?? []) {
      if (deletedLocal.has(path)) continue;
      deletedLocal.add(path);
      await bestEffort(`render orphan local cleanup ${attempt.attemptId}`, async () => { await removeLocal(path); });
    }
    for (const key of attempt.objectKeys ?? []) {
      if (!removeObject || deletedObjects.has(key)) continue;
      deletedObjects.add(key);
      await bestEffort(`render orphan object cleanup ${attempt.attemptId}`, async () => { await removeObject(key); });
    }
  }
};

export type AttemptRetentionJob = {
  runNow(): Promise<void>;
  close(): Promise<void>;
};

/** Starts an operational retention sweep; the caller supplies durable winner-aware enumeration. */
export const startAttemptRetentionJob = (options: Omit<RenderAttemptCleanup, 'winnerAttemptId' | 'losingAttempts'> & {
  intervalMs: number;
  enumerate: () => Promise<Array<Pick<RenderAttemptCleanup, 'winnerAttemptId' | 'losingAttempts'>>>;
}): AttemptRetentionJob => {
  let closed = false;
  let active = Promise.resolve();
  const runNow = async (): Promise<void> => {
    if (closed) return;
    active = active.then(async () => {
      const batches = await withDeadline('render attempt retention enumeration', options.timeoutMs, async () => await options.enumerate()).catch(() => []);
      for (const batch of batches) await cleanupKnownLosingAttempts({...options, ...batch});
    });
    await active;
  };
  const timer = setInterval(() => { void runNow().catch(() => undefined); }, options.intervalMs);
  timer.unref();
  return {runNow, close: async () => { closed = true; clearInterval(timer); await active; }};
};
