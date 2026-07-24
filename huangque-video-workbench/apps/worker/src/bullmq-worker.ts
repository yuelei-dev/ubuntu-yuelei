import {UnrecoverableError, Worker, type ConnectionOptions, type WorkerOptions} from 'bullmq';
import {canonicalInputHash, type ProjectEventPublisher, type ProjectRepository} from '@huangque/api';
import {
  DeliveryAttemptsExhaustedError,
  DeliveryClaimConflictError,
  GeneratedMediaQualityError,
  PipelineConfigurationError,
  PipelineService,
  RenderJobTerminalError,
  TransientProviderError,
  createSceneJobConsumer,
  type PipelineDependencies,
  type PipelineRepository,
  Task4PipelineRepository,
  type SceneJobConsumer,
  type SceneJobConsumerDependencies
} from './pipeline.js';
import {UnknownJobNameError} from './processors/registry.js';

export type BullMqSceneJobData = {
  projectId: string;
  sceneId: string;
  businessJobId: string;
  input?: unknown;
};

export type BullMqSceneDelivery = {
  name: string;
  data: BullMqSceneJobData;
  attemptsMade: number;
  signal?: AbortSignal;
};

/** Converts one BullMQ delivery into one durable, named processor dispatch. */
export const createBullMqWorkerProcessor = (dependencies: {
  repository: PipelineRepository;
  consumer: SceneJobConsumer;
  onSceneResult?: (projectId: string) => Promise<void>;
  onSceneTerminalFailure?: (projectId: string, sceneId: string, generationJobId: string, failureReason: string) => Promise<void>;
}) => async (delivery: BullMqSceneDelivery): Promise<void> => {
  const {businessJobId} = delivery.data;
  if (!businessJobId) throw new UnrecoverableError('BullMQ scene job is missing its durable businessJobId');

  let job;
  try {
    job = await dependencies.repository.findJob(businessJobId);
  } catch (error) {
    throw new UnrecoverableError(error instanceof Error ? error.message : `durable job ${businessJobId} was not found`);
  }

  if (delivery.name !== job.name ||
      delivery.data.projectId !== job.data.projectId ||
      delivery.data.sceneId !== job.data.sceneId) {
    throw new UnrecoverableError(`BullMQ delivery metadata does not match durable job ${businessJobId}`);
  }

  try {
    await dependencies.consumer.consume({
      job,
      attemptsMade: delivery.attemptsMade
    });
    await dependencies.onSceneResult?.(job.data.projectId);
  } catch (error) {
    if (error instanceof GeneratedMediaQualityError || error instanceof DeliveryAttemptsExhaustedError) {
      await dependencies.onSceneTerminalFailure?.(job.data.projectId, job.data.sceneId, job.id, error.message);
      throw new UnrecoverableError(error.message);
    }
    if (error instanceof DeliveryClaimConflictError || error instanceof UnknownJobNameError) {
      throw new UnrecoverableError(error.message);
    }
    if (error instanceof TransientProviderError) {
      const durable = await dependencies.repository.findJob(job.id);
      if ((durable.options.deliveryAttemptsMade ?? 0) >= durable.options.attempts) {
        await dependencies.onSceneTerminalFailure?.(job.data.projectId, job.data.sceneId, job.id, error.message);
        throw new UnrecoverableError(error.message);
      }
      throw error.providerCause instanceof Error
        ? error.providerCause
        : new Error(error.message, {cause: error.providerCause});
    }
    throw error;
  }
};

export const createBullMqSceneWorker = (options: {
  queueName: string;
  connection: ConnectionOptions;
  workerOptions?: Omit<WorkerOptions, 'connection'>;
} & SceneJobConsumerDependencies): Worker<BullMqSceneJobData> => {
  const consumer = createSceneJobConsumer(options);
  return new Worker<BullMqSceneJobData>(
    options.queueName,
    createBullMqWorkerProcessor({
      repository: options.repository,
      consumer,
      onSceneTerminalFailure: async (projectId, sceneId, generationJobId, failureReason) => {
        await options.repository.markSceneFailedAndProjectPartiallyFailed(projectId, sceneId, generationJobId, failureReason);
      }
    }),
    {...options.workerOptions, connection: options.connection}
  );
};

type BullMqPipelineDependencies = SceneJobConsumerDependencies & Pick<PipelineDependencies,
  'queue' | 'buildStoryboard' | 'onProjectStatusPersisted' | 'qualityInspector' | 'renderDeadlineMs' | 'renderLeaseMs' | 'renderLeaseHeartbeatMs' | 'now' | 'renderFailureReportDeadlineMs' | 'renderFailureReportWriter'>;

/** Production processor for the shared Task 4 queue. */
export const createBullMqPipelineProcessor = (dependencies: BullMqPipelineDependencies) => {
  if (!dependencies.qualityInspector) throw new PipelineConfigurationError('a quality inspector is required for the production pipeline');
  const service = new PipelineService({...dependencies, deferSceneJobs: true});
  const consumer = createSceneJobConsumer(dependencies);
  const sceneProcessor = createBullMqWorkerProcessor({
    repository: dependencies.repository,
    consumer,
    onSceneResult: async (projectId) => { await service.checkProjectFanIn(projectId); },
    onSceneTerminalFailure: async (projectId, sceneId, generationJobId, failureReason) => {
      await service.markSceneTerminalFailure(projectId, sceneId, generationJobId, failureReason);
    }
  });

  const loadControlJob = async (delivery: BullMqSceneDelivery) => {
    let job;
    try {
      job = await dependencies.repository.findJobMetadata(delivery.data.businessJobId);
    } catch (error) {
      throw new UnrecoverableError(error instanceof Error ? error.message : 'durable regeneration job was not found');
    }
    if (delivery.name !== job.name ||
        delivery.data.projectId !== job.data.projectId ||
        delivery.data.sceneId !== job.data.sceneId) {
      throw new UnrecoverableError(`BullMQ delivery metadata does not match durable job ${delivery.data.businessJobId}`);
    }
    const controlInput = delivery.data.input;
    if (!controlInput || typeof controlInput !== 'object' || Array.isArray(controlInput)) {
      throw new UnrecoverableError(`BullMQ regeneration job ${delivery.data.businessJobId} has invalid input`);
    }
    if (canonicalInputHash(controlInput) !== job.inputHash) {
      throw new UnrecoverableError(`BullMQ delivery input does not match durable job ${delivery.data.businessJobId}`);
    }
    const visual = (controlInput as {visual?: unknown}).visual;
    if (!visual || typeof visual !== 'object' || Array.isArray(visual)) {
      throw new UnrecoverableError(`BullMQ regeneration job ${delivery.data.businessJobId} has invalid scene visual input`);
    }
    const expectedGenerationJobId = (visual as {activeGenerationJobId?: unknown}).activeGenerationJobId;
    if (expectedGenerationJobId !== undefined && typeof expectedGenerationJobId !== 'string') {
      throw new UnrecoverableError(`BullMQ regeneration job ${delivery.data.businessJobId} has invalid active generation identity`);
    }
    return {job, expectedGenerationJobId};
  };

  const loadRenderJob = async (delivery: BullMqSceneDelivery) => {
    let job;
    try {
      job = await dependencies.repository.findJobMetadata(delivery.data.businessJobId);
    } catch (error) {
      throw new UnrecoverableError(error instanceof Error ? error.message : 'durable render job was not found');
    }
    if (delivery.name !== job.name ||
        delivery.data.projectId !== job.data.projectId ||
        delivery.data.sceneId !== job.data.sceneId) {
      throw new UnrecoverableError(`BullMQ delivery metadata does not match durable job ${delivery.data.businessJobId}`);
    }
    const input = delivery.data.input;
    if (!input || typeof input !== 'object' || Array.isArray(input) || canonicalInputHash(input) !== job.inputHash) {
      throw new UnrecoverableError(`BullMQ delivery input does not match durable job ${delivery.data.businessJobId}`);
    }
    return job;
  };

  return async (delivery: BullMqSceneDelivery): Promise<void> => {
    if (delivery.name === 'project.render') {
      const job = await loadRenderJob(delivery);
      try {
        await service.processRenderJob(job.data.projectId, job.id, delivery.signal ?? new AbortController().signal);
      } catch (error) {
        if (error instanceof RenderJobTerminalError) throw new UnrecoverableError(error.message);
        throw error;
      }
      return;
    }
    if (delivery.name === 'storyboard.generate') {
      await service.runProjectPipeline(delivery.data.projectId);
      return;
    }
    if (delivery.name === 'scene.asset.generate' || delivery.name === 'scene.avatar.generate') {
      await sceneProcessor(delivery);
      return;
    }
    if (delivery.name === 'scene.regenerate') {
      const {job, expectedGenerationJobId} = await loadControlJob(delivery);
      await service.regenerateScene(job.data.projectId, job.data.sceneId, job.id, expectedGenerationJobId);
      return;
    }
    throw new UnrecoverableError(`no production processor registered for job name ${delivery.name}`);
  };
};

export const createBullMqPipelineJobProcessor = (dependencies: BullMqPipelineDependencies) => {
  const dispatch = createBullMqPipelineProcessor(dependencies);
  return async (
    job: Pick<BullMqSceneDelivery, 'name' | 'data' | 'attemptsMade'>,
    _token?: string,
    signal?: AbortSignal
  ): Promise<void> => dispatch({...job, signal});
};

export const createBullMqPipelineWorker = (options: {
  queueName: string;
  connection: ConnectionOptions;
  workerOptions?: Omit<WorkerOptions, 'connection'>;
} & BullMqPipelineDependencies): Worker<BullMqSceneJobData> => new Worker<BullMqSceneJobData>(
  options.queueName,
  createBullMqPipelineJobProcessor(options),
  {...options.workerOptions, connection: options.connection}
);

/** Production composition keeps BullMQ separate from the cross-process event publisher. */
export const createProductionWorkerComposition = (options: Omit<Parameters<typeof createBullMqPipelineWorker>[0], 'repository'> & {
  repository: ProjectRepository;
  publisher: ProjectEventPublisher;
  now?: () => Date;
  workerFactory?: (options: Parameters<typeof createBullMqPipelineWorker>[0]) => Worker<BullMqSceneJobData>;
}) => {
  const {workerFactory = createBullMqPipelineWorker, now, publisher, repository, ...workerOptions} = options;
  return workerFactory({...workerOptions, repository: new Task4PipelineRepository(repository, now ?? (() => new Date()), publisher)});
};
