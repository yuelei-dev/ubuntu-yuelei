import {describe, expect, it, vi} from 'vitest';
import {FixtureTimeoutError, withDeadline} from './deadline.js';

describe('fixture deadlines', () => {
  it('returns work that finishes before the deadline', async () => {
    await expect(withDeadline('fast operation', 100, async () => 'done')).resolves.toBe('done');
  });

  it('throws a typed timeout and runs cleanup exactly once', async () => {
    const cleanup = vi.fn(async () => undefined);
    const failure = withDeadline('stuck renderer', 10, async () => new Promise<string>(() => undefined), cleanup);

    await expect(failure).rejects.toMatchObject<Partial<FixtureTimeoutError>>({
      name: 'FixtureTimeoutError',
      operation: 'stuck renderer',
      timeoutMs: 10
    });
    expect(cleanup).toHaveBeenCalledOnce();
  });

  it('does not let a stuck cleanup defeat the operation deadline', async () => {
    await expect(withDeadline(
      'stuck cleanup',
      10,
      async () => new Promise<string>(() => undefined),
      async () => new Promise<void>(() => undefined)
    )).rejects.toBeInstanceOf(FixtureTimeoutError);
  });
});
