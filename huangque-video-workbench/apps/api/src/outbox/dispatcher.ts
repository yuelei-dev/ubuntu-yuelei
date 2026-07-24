import {randomUUID} from 'node:crypto';
import type {QueueAdapter} from '../queue.js';
import type {JobRecord, ProjectRepository} from '../services/project-service.js';

export type OutboxDispatcherOptions = {
  pollIntervalMs?: number;
  leaseDurationMs?: number;
  batchSize?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  now?: () => Date;
  owner?: string;
};

const abortError = (): Error => Object.assign(new Error('outbox dispatch aborted'), {name: 'AbortError'});

export class OutboxDispatcher {
  private readonly owner: string;
  private readonly pollIntervalMs: number;
  private readonly leaseDurationMs: number;
  private readonly batchSize: number;
  private readonly baseBackoffMs: number;
  private readonly maxBackoffMs: number;
  private readonly now: () => Date;
  private timer?: NodeJS.Timeout;
  private active?: Promise<number>;
  private stopped = false;
  private readonly controller = new AbortController();

  constructor(
    private readonly repository: ProjectRepository,
    private readonly queue: QueueAdapter,
    options: OutboxDispatcherOptions = {}
  ) {
    this.owner = options.owner ?? randomUUID();
    this.pollIntervalMs = options.pollIntervalMs ?? 5_000;
    this.leaseDurationMs = options.leaseDurationMs ?? 30_000;
    this.batchSize = options.batchSize ?? 50;
    this.baseBackoffMs = options.baseBackoffMs ?? 1_000;
    this.maxBackoffMs = options.maxBackoffMs ?? 5 * 60_000;
    this.now = options.now ?? (() => new Date());
  }

  start(): void {
    if (this.timer || this.stopped) return;
    const poll = () => {
      if (this.active || this.stopped) return;
      this.active = this.dispatchOnce(this.controller.signal)
        .catch(() => 0)
        .finally(() => { this.active = undefined; });
    };
    poll();
    this.timer = setInterval(poll, this.pollIntervalMs);
    this.timer.unref();
  }

  async dispatchOnce(signal: AbortSignal = this.controller.signal): Promise<number> {
    if (signal.aborted) throw abortError();
    const claimed = await this.repository.claimDueJobs(this.owner, this.batchSize, this.leaseDurationMs);
    let dispatched = 0;
    for (const job of claimed) {
      if (signal.aborted) throw abortError();
      try {
        await this.queue.submit({
          id: job.id,
          name: job.taskType,
          data: {projectId: job.projectId, sceneId: job.sceneId, input: job.dispatchPayload},
          ...(job.options ? {options: job.options} : {})
        });
        if (await this.repository.markJobDispatched(job.id, this.owner)) dispatched++;
      } catch {
        const exponent = Math.min(job.dispatchAttempts ?? 0, 30);
        const delay = Math.min(this.maxBackoffMs, this.baseBackoffMs * (2 ** exponent));
        await this.repository.recordJobDispatchFailure(job.id, this.owner, new Date(this.now().getTime() + delay));
      }
    }
    return dispatched;
  }

  async close(deadlineMs: number): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    this.controller.abort();
    if (!this.active) return;
    let timeout: NodeJS.Timeout | undefined;
    try {
      await Promise.race([
        this.active.then(() => undefined, () => undefined),
        new Promise<void>((resolve) => {
          timeout = setTimeout(resolve, Math.max(0, deadlineMs));
          timeout.unref();
        })
      ]);
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }
}
