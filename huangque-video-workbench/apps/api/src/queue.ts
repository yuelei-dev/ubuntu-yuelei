import {createHash} from 'node:crypto';
import type {Queue} from 'bullmq';

export type QueueJob = {
  id: string;
  name: string;
  data: Record<string, unknown>;
  options?: {
    attempts: number;
    backoff: {type: 'exponential'; delay: number};
    /** Worker-owned durable generation state; never forwarded as a BullMQ option. */
    qualityAttempt?: number;
    deliveryAttemptsMade?: number;
    qualityTerminal?: boolean;
  };
};

export type QueueSubmission = {id: string; existing: boolean};

export interface QueueAdapter {
  submit(job: QueueJob): Promise<QueueSubmission>;
}

export const jobKey = (projectId: string, sceneId: string, taskType: string, inputHash: string) => `${projectId}:${sceneId}:${taskType}:${inputHash}`;

/** BullMQ prohibits colons in custom IDs; the business key remains durable. */
export const bullMqJobId = (businessJobKey: string): string => createHash('sha256').update(businessJobKey).digest('hex');

export class InMemoryQueue implements QueueAdapter {
  private readonly submitted = new Map<string, QueueJob>();

  async submit(job: QueueJob): Promise<QueueSubmission> {
    if (this.submitted.has(job.id)) return {id: job.id, existing: true};
    this.submitted.set(job.id, structuredClone(job));
    return {id: job.id, existing: false};
  }

  jobs(): QueueJob[] {
    return [...this.submitted.values()].map((job) => structuredClone(job));
  }
}

type BullMqQueue = {
  add(name: string, data: Record<string, unknown>, options: {jobId: string; attempts?: number; backoff?: {type: 'exponential'; delay: number}}): Promise<{id?: string | null}>;
};

/** Adapter boundary: construct with a real BullMQ Queue in production. */
export class BullMqQueueAdapter implements QueueAdapter {
  constructor(private readonly queue: BullMqQueue) {}

  async submit(job: QueueJob): Promise<QueueSubmission> {
    const retryOptions = job.options && {attempts: job.options.attempts, backoff: job.options.backoff};
    await this.queue.add(job.name, {...job.data, businessJobId: job.id}, {jobId: bullMqJobId(job.id), ...retryOptions});
    return {id: job.id, existing: false};
  }
}

/** Connects the queue boundary to a real BullMQ Queue at the composition root. */
export const createBullMqQueueAdapter = (queue: Queue): BullMqQueueAdapter => new BullMqQueueAdapter(queue);
