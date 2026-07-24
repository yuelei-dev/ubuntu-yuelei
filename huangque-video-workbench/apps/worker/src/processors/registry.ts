export type SceneJobName = 'scene.asset.generate' | 'scene.avatar.generate';

export type SceneJob = {
  id: string;
  name: string;
  data: {projectId: string; sceneId: string};
  options: {
    attempts: number;
    backoff: {type: 'exponential'; delay: number};
    qualityAttempt: number;
    contentRevision: number;
    deliveryAttemptsMade?: number;
    qualityTerminal?: boolean;
  };
  status: 'PENDING' | 'QUEUED';
};

export interface SceneJobProcessor {
  process(job: SceneJob): Promise<void>;
}

type RegisteredProcessors = Record<SceneJobName, SceneJobProcessor>;

export class UnknownJobNameError extends Error {
  readonly name = 'UnknownJobNameError';

  constructor(readonly jobName: string) {
    super(`no processor registered for job name ${jobName}`);
  }
}

/** Explicit queue-worker dispatch boundary keyed by the BullMQ job name. */
export class JobNameProcessorRegistry {
  constructor(private readonly processors: RegisteredProcessors) {}

  async dispatch(job: SceneJob): Promise<void> {
    const processor = this.processors[job.name as SceneJobName];
    if (!processor) throw new UnknownJobNameError(job.name);
    await processor.process(job);
  }
}
