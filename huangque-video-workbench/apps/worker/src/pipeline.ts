import {SceneSchema, type ProjectStatus, type Scene, type Storyboard} from '@huangque/contracts';
import {randomUUID} from 'node:crypto';
import {mkdir, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {assertAllowedProvenance, type AvatarProvider, type GeneratedAsset, type ImageProvider} from '@huangque/providers';
import {inspectOutput, qualityReportPath, type QualityExpectation, type QualityReport} from '@huangque/media';
import {canonicalInputHash, InMemoryProjectRepository, jobKey, type AssetVersionRecord, type JobRecord, type ProjectEventPublisher, type ProjectRecord, type ProjectRepository, type QueueAdapter, type RegenerationProjectStatus, type RenderJobTerminalCommit} from '@huangque/api';
import {createAvatarGenerateProcessor, isAvatarScene} from './processors/avatar.js';
import {createAssetGenerateProcessor, isMissingFactualAsset} from './processors/assets.js';
import {allRequiredScenesReady} from './processors/render.js';
import {JobNameProcessorRegistry, type SceneJob} from './processors/registry.js';
import {buildProjectStoryboard} from './processors/storyboard.js';
import {transitionProject} from './state-transitions.js';

export type PipelineSceneStatus = 'PENDING' | 'GENERATING' | 'READY' | 'FALLBACK_ACCEPTED' | 'NEEDS_USER_INPUT' | 'FAILED';

export type PipelineProject = {
  id: string;
  title: string;
  script: string;
  status: ProjectStatus;
  avatar: {avatarId: string; voiceId: string};
  output: {templateId: string};
  qualityReportPath?: string;
};

export type PipelineScene = Scene & {
  projectId: string;
  status: PipelineSceneStatus;
  assetVersions: GeneratedAsset[];
  activeGenerationJobId?: string;
  contentRevision: number;
};

export type PipelineJob = SceneJob;
export type PipelineJobMetadata = Pick<PipelineJob, 'id' | 'name' | 'data'> & {status: JobRecord['status']; inputHash: string};

export interface PipelineRepository {
  findProject(projectId: string): Promise<PipelineProject | undefined>;
  updateProjectStatus(projectId: string, status: ProjectStatus): Promise<void>;
  claimProjectStatus(projectId: string, expectedStatus: ProjectStatus, nextStatus: ProjectStatus): Promise<PipelineProject | undefined>;
  claimProjectQualityResult(projectId: string, nextStatus: Extract<ProjectStatus, 'COMPLETED' | 'FAILED'>, qualityReportPath: string, output?: {previewUrl?: string; downloadUrl?: string}): Promise<PipelineProject | undefined>;
  markSceneFailedAndProjectPartiallyFailed(projectId: string, sceneId: string, expectedGenerationJobId: string, failureReason?: string): Promise<{project: PipelineProject; transitioned: boolean; sceneUpdated: boolean} | undefined>;
  persistScenes(projectId: string, scenes: Scene[]): Promise<void>;
  listScenes(projectId: string): Promise<PipelineScene[]>;
  updateSceneStatus(projectId: string, sceneId: string, status: PipelineSceneStatus): Promise<void>;
  activateSceneRegeneration(projectId: string, sceneId: string, expectedGenerationJobId: string | undefined, nextGenerationJobId: string, expectedContentRevision: number, nextProjectStatus: RegenerationProjectStatus): Promise<{project: PipelineProject; transitioned: boolean} | undefined>;
  reserveAndActivateSceneRegeneration(job: PipelineJob, expectedGenerationJobId: string | undefined, expectedContentRevision: number, nextProjectStatus: RegenerationProjectStatus): Promise<{project: PipelineProject; job: PipelineJob; existing: boolean; transitioned: boolean} | undefined>;
  claimSceneActiveGenerationJob(projectId: string, sceneId: string, expectedGenerationJobId: string | undefined, nextGenerationJobId: string, expectedContentRevision: number): Promise<boolean>;
  beginSceneGeneration(projectId: string, sceneId: string, generationJobId: string, expectedContentRevision: number): Promise<boolean>;
  completeSceneGeneration(projectId: string, sceneId: string, generationJobId: string, expectedContentRevision: number, asset: GeneratedAsset): Promise<boolean>;
  addAssetVersion(projectId: string, sceneId: string, asset: GeneratedAsset): Promise<void>;
  reserveJob(job: PipelineJob): Promise<{job: PipelineJob; existing: boolean}>;
  findJob(id: string): Promise<PipelineJob>;
  findJobMetadata(id: string): Promise<PipelineJobMetadata>;
  claimJobDelivery(id: string, expectedDeliveryAttemptsMade: number, nextDeliveryAttemptsMade: number): Promise<PipelineJob | undefined>;
  claimJobQuality(id: string, expectedQualityAttempt: number, patch: {qualityAttempt?: number; qualityTerminal?: boolean}): Promise<PipelineJob | undefined>;
  markJobQueued(id: string): Promise<PipelineJobMetadata | undefined>;
  updateJobQualityAttempt(id: string, qualityAttempt: number): Promise<void>;
  updateJobGenerationState(id: string, patch: {qualityAttempt?: number; deliveryAttemptsMade?: number; qualityTerminal?: boolean}): Promise<void>;
  claimRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<PipelineJobMetadata | undefined>;
  renewRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<PipelineJobMetadata | undefined>;
  commitRenderJobTerminal(commit: RenderJobTerminalCommit): Promise<{project: PipelineProject; job: PipelineJobMetadata} | undefined>;
}

const clone = <T>(value: T): T => structuredClone(value);

const asPipelineProject = (project: ProjectRecord): PipelineProject => ({
  id: project.id,
  title: project.title,
  script: project.input.content,
  status: project.status,
  avatar: project.avatar,
  output: project.output,
  ...(project.qualityReportPath ? {qualityReportPath: project.qualityReportPath} : {})
});

const asPipelineJob = (job: JobRecord): PipelineJob => {
  if (!job.options) throw new Error(`durable scene job ${job.id} has no generation options`);
  return {
    id: job.id,
    name: job.taskType,
    data: {projectId: job.projectId, sceneId: job.sceneId},
    options: {
      attempts: job.options.attempts,
      backoff: job.options.backoff,
      qualityAttempt: job.options.qualityAttempt ?? 1,
      contentRevision: job.options.contentRevision ?? 0,
      deliveryAttemptsMade: job.options.deliveryAttemptsMade,
      qualityTerminal: job.options.qualityTerminal
    },
    status: job.status as PipelineJob['status']
  };
};

/** Worker view over the durable Task 4 repository; it owns no project, job, or asset state. */
export class Task4PipelineRepository implements PipelineRepository {
  constructor(private readonly repository: ProjectRepository, private readonly now: () => Date = () => new Date(), private readonly publisher?: ProjectEventPublisher) {}
  private async publish(projectId: string): Promise<void> { const project = await this.repository.findProjectForWorker(projectId); if (project) this.publisher?.publish(project); }

  async findProject(projectId: string): Promise<PipelineProject | undefined> {
    const project = await this.repository.findProjectForWorker(projectId);
    return project && asPipelineProject(project);
  }

  async updateProjectStatus(projectId: string, status: ProjectStatus): Promise<void> { await this.repository.updateProjectStatus(projectId, status); await this.publish(projectId); }

  async claimProjectStatus(
    projectId: string,
    expectedStatus: ProjectStatus,
    nextStatus: ProjectStatus
  ): Promise<PipelineProject | undefined> {
    const claimed = await this.repository.claimProjectStatus(projectId, expectedStatus, nextStatus);
    if (claimed) await this.publish(projectId); return claimed && asPipelineProject(claimed);
  }

  async claimProjectQualityResult(
    projectId: string,
    nextStatus: Extract<ProjectStatus, 'COMPLETED' | 'FAILED'>,
    qualityReportPath: string, output?: {previewUrl?: string; downloadUrl?: string}
  ): Promise<PipelineProject | undefined> {
    const claimed = await this.repository.claimProjectQualityResult(projectId, nextStatus, qualityReportPath, output);
    if (claimed) await this.publish(projectId); return claimed && asPipelineProject(claimed);
  }

  async markSceneFailedAndProjectPartiallyFailed(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string, failureReason?: string
  ): Promise<{project: PipelineProject; transitioned: boolean; sceneUpdated: boolean} | undefined> {
    const result = await this.repository.markSceneFailedAndProjectPartiallyFailed(projectId, sceneId, expectedGenerationJobId, failureReason);
    if (result?.sceneUpdated) await this.publish(projectId); return result && {project: asPipelineProject(result.project), transitioned: result.transitioned, sceneUpdated: result.sceneUpdated};
  }

  async persistScenes(projectId: string, scenes: Scene[]): Promise<void> {
    const existing = await this.repository.findProjectForWorker(projectId);
    const known = new Set(existing?.scenes.map((scene) => scene.id));
    let inserted = 0; for (const scene of scenes) {
      if (known.has(scene.id)) continue;
      const now = this.now().toISOString();
      await this.repository.createScene({id: scene.id, projectId, order: scene.order, status: 'PENDING', script: scene.script,
        visual: {
          ...scene.visual,
          sceneType: scene.type,
          purpose: scene.purpose,
          durationEstimate: scene.durationEstimate,
          contentRevision: 0,
          ...(scene.visualPrompt === undefined ? {} : {visualPrompt: scene.visualPrompt})
        }, asset: scene.asset ?? null, createdAt: now, updatedAt: now});
      inserted += 1;
    }
    if (inserted > 0) await this.publish(projectId);
  }

  async listScenes(projectId: string): Promise<PipelineScene[]> {
    const project = await this.repository.findProjectForWorker(projectId);
    if (!project) return [];
    return project.scenes.map((scene) => {
      const {sceneType, purpose, durationEstimate, visualPrompt, activeGenerationJobId, contentRevision, workerScene, canonicalSceneQuarantine, ...presentation} = scene.visual;
      if (typeof canonicalSceneQuarantine === 'string') {
        throw new CanonicalSceneQuarantinedError(scene.id, canonicalSceneQuarantine);
      }
      const legacy = workerScene && typeof workerScene === 'object' && !Array.isArray(workerScene)
        ? workerScene as Partial<Scene>
        : undefined;
      const parsed = SceneSchema.safeParse({
        id: scene.id,
        order: scene.order,
        type: sceneType ?? legacy?.type,
        purpose: purpose ?? legacy?.purpose,
        script: scene.script,
        durationEstimate: durationEstimate ?? legacy?.durationEstimate,
        visualPrompt,
        visual: presentation,
        asset: scene.asset
      });
      if (!parsed.success) {
        throw new CanonicalSceneQuarantinedError(scene.id, parsed.error.issues.map((issue) => issue.message).join('; '));
      }
      const canonical = parsed.data;
      return {...canonical, projectId, status: scene.status as PipelineSceneStatus,
        activeGenerationJobId: typeof activeGenerationJobId === 'string' ? activeGenerationJobId : undefined,
        contentRevision: typeof contentRevision === 'number' ? contentRevision : 0,
        assetVersions: project.assetVersions.filter((asset) => asset.sceneId === scene.id).map((asset) => ({uri: asset.uri, provenance: asset.provenance, inputHash: asset.inputHash, width: 1080, height: 1920}))};
    });
  }

  async updateSceneStatus(projectId: string, sceneId: string, status: PipelineSceneStatus): Promise<void> {
    const updated = await this.repository.updateSceneForWorker(projectId, sceneId, {status}); if (updated) await this.publish(projectId);
  }

  async activateSceneRegeneration(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string | undefined,
    nextGenerationJobId: string,
    expectedContentRevision: number,
    nextProjectStatus: RegenerationProjectStatus
  ): Promise<{project: PipelineProject; transitioned: boolean} | undefined> {
    const result = await this.repository.activateSceneRegeneration(
      projectId, sceneId, expectedGenerationJobId, nextGenerationJobId, expectedContentRevision, nextProjectStatus
    );
    if (result) await this.publish(projectId); return result && {project: asPipelineProject(result.project), transitioned: result.transitioned};
  }

  async reserveAndActivateSceneRegeneration(
    job: PipelineJob,
    expectedGenerationJobId: string | undefined,
    expectedContentRevision: number,
    nextProjectStatus: RegenerationProjectStatus
  ): Promise<{project: PipelineProject; job: PipelineJob; existing: boolean; transitioned: boolean} | undefined> {
    const inputHash = job.id.split(':').at(-1) ?? '';
    const result = await this.repository.reserveAndActivateSceneRegeneration({
      id: job.id,
      projectId: job.data.projectId as string,
      sceneId: job.data.sceneId as string,
      taskType: job.name,
      inputHash,
      dispatchPayload: job.data,
      status: job.status,
      options: job.options,
      createdAt: this.now().toISOString()
    }, expectedGenerationJobId, expectedContentRevision, nextProjectStatus);
    if (!result) return undefined;
    await this.publish(job.data.projectId as string);
    return {
      project: asPipelineProject(result.project),
      job: {...job, status: result.job.status as PipelineJob['status']},
      existing: result.existing,
      transitioned: result.transitioned
    };
  }

  async claimSceneActiveGenerationJob(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string | undefined,
    nextGenerationJobId: string,
    expectedContentRevision: number
  ): Promise<boolean> {
    const claimed = Boolean(await this.repository.claimSceneActiveGenerationJob(
      projectId, sceneId, expectedGenerationJobId, nextGenerationJobId, expectedContentRevision
    )); if (claimed) await this.publish(projectId); return claimed;
  }

  async beginSceneGeneration(projectId: string, sceneId: string, generationJobId: string, expectedContentRevision: number): Promise<boolean> {
    const begun = Boolean(await this.repository.beginSceneGeneration(projectId, sceneId, generationJobId, expectedContentRevision));
    if (begun) await this.publish(projectId); return begun;
  }

  async completeSceneGeneration(
    projectId: string,
    sceneId: string,
    generationJobId: string,
    expectedContentRevision: number,
    asset: GeneratedAsset
  ): Promise<boolean> {
    const completed = Boolean(await this.repository.completeSceneGeneration(projectId, sceneId, generationJobId, expectedContentRevision, {
      uri: asset.uri,
      provenance: asset.provenance,
      inputHash: asset.inputHash,
      createdAt: this.now().toISOString()
    })); if (completed) await this.publish(projectId); return completed;
  }

  async addAssetVersion(projectId: string, sceneId: string, asset: GeneratedAsset): Promise<void> {
    const versions = await this.repository.listAssetVersions(projectId, sceneId);
    const record: AssetVersionRecord = {id: `${projectId}:${sceneId}:v${versions.length + 1}`, projectId, sceneId, version: versions.length + 1,
      uri: asset.uri, provenance: asset.provenance, inputHash: asset.inputHash, createdAt: this.now().toISOString()};
    await this.repository.createAssetVersion(record);
    await this.publish(projectId);
  }

  async reserveJob(job: PipelineJob): Promise<{job: PipelineJob; existing: boolean}> {
    const inputHash = job.id.split(':').at(-1) ?? '';
    const reserved = await this.repository.reserveJob({id: job.id, projectId: job.data.projectId as string, sceneId: job.data.sceneId as string,
      taskType: job.name, inputHash, dispatchPayload: job.data, status: job.status, options: job.options, createdAt: this.now().toISOString()});
    return {existing: reserved.existing, job: {...job, status: reserved.job.status as PipelineJob['status'], options: {
      ...job.options,
      ...reserved.job.options,
      qualityAttempt: reserved.job.options?.qualityAttempt ?? job.options.qualityAttempt
    }}};
  }

  async findJob(id: string): Promise<PipelineJob> {
    const job = await this.repository.findJob(id);
    if (!job) throw new Error(`durable scene job ${id} was not found`);
    return asPipelineJob(job);
  }

  async findJobMetadata(id: string): Promise<PipelineJobMetadata> {
    const job = await this.repository.findJob(id);
    if (!job) throw new Error(`durable job ${id} was not found`);
    return {
      id: job.id,
      name: job.taskType,
      data: {projectId: job.projectId, sceneId: job.sceneId},
      status: job.status,
      inputHash: job.inputHash
    };
  }

  async claimJobDelivery(id: string, expectedDeliveryAttemptsMade: number, nextDeliveryAttemptsMade: number): Promise<PipelineJob | undefined> {
    const claimed = await this.repository.claimJobDelivery(id, expectedDeliveryAttemptsMade, nextDeliveryAttemptsMade);
    return claimed && asPipelineJob(claimed);
  }

  async claimJobQuality(
    id: string,
    expectedQualityAttempt: number,
    patch: {qualityAttempt?: number; qualityTerminal?: boolean}
  ): Promise<PipelineJob | undefined> {
    const claimed = await this.repository.claimJobQuality(id, expectedQualityAttempt, patch);
    return claimed && asPipelineJob(claimed);
  }

  async markJobQueued(id: string): Promise<PipelineJobMetadata | undefined> {
    const job = await this.repository.markJobQueued(id);
    return job && {id: job.id, name: job.taskType, data: {projectId: job.projectId, sceneId: job.sceneId}, status: job.status, inputHash: job.inputHash};
  }

  updateJobQualityAttempt(id: string, qualityAttempt: number): Promise<void> {
    return this.repository.updateJobQualityAttempt(id, qualityAttempt);
  }

  updateJobGenerationState(id: string, patch: {qualityAttempt?: number; deliveryAttemptsMade?: number; qualityTerminal?: boolean}): Promise<void> {
    return this.repository.updateJobGenerationState(id, patch);
  }

  async claimRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<PipelineJobMetadata | undefined> {
    const job = await this.repository.claimRenderJobLease(id, owner, leaseDurationMs);
    return job && {id: job.id, name: job.taskType, data: {projectId: job.projectId, sceneId: job.sceneId}, status: job.status, inputHash: job.inputHash};
  }

  async renewRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<PipelineJobMetadata | undefined> {
    const job = await this.repository.renewRenderJobLease(id, owner, leaseDurationMs);
    return job && {id: job.id, name: job.taskType, data: {projectId: job.projectId, sceneId: job.sceneId}, status: job.status, inputHash: job.inputHash};
  }

  async commitRenderJobTerminal(commit: RenderJobTerminalCommit): Promise<{project: PipelineProject; job: PipelineJobMetadata} | undefined> {
    const result = await this.repository.commitRenderJobTerminal(commit);
    if (result) await this.publish(commit.projectId);
    return result && {
      project: asPipelineProject(result.project),
      job: {id: result.job.id, name: result.job.taskType, data: {projectId: result.job.projectId, sceneId: result.job.sceneId}, status: result.job.status, inputHash: result.job.inputHash}
    };
  }
}

/** Test adapter backed by the same Task 4 in-memory repository implementation. */
export class InMemoryPipelineRepository extends Task4PipelineRepository {
  constructor(projects: PipelineProject[]) {
    const repository = new InMemoryProjectRepository();
    for (const project of projects) {
      const now = new Date().toISOString();
      const record: ProjectRecord = {id: project.id, ownerUsername: '__pipeline_test_owner__', title: project.title, status: project.status, input: {type: 'script', content: project.script},
        avatar: {avatarId: 'mock', voiceId: 'mock'}, output: {templateId: 'vertical_knowledge_v1'}, createdAt: now, updatedAt: now};
      void repository.createProject(record);
    }
    super(repository);
  }
}

export type PipelineDependencies = {
  repository: PipelineRepository;
  queue: QueueAdapter;
  buildStoryboard: (input: {title: string; script: string}) => Storyboard;
  avatarProvider: AvatarProvider;
  imageProvider: ImageProvider;
  qualityInspector?: OutputQualityInspector;
  onRetry?: (metadata: {projectId: string; sceneId: string; attempt: number; delayMs: number}) => void;
  assessGeneratedMedia?: (input: {scene: PipelineScene; asset: GeneratedAsset}) => {accepted: boolean; reason?: string};
  /** Offline tests inject a BullMQ delivery runner; production uses bullmq-worker.ts. */
  jobRunner?: SceneJobRunner;
  /** Production orchestration submits scene jobs and lets BullMQ workers complete them. */
  deferSceneJobs?: boolean;
  /** Hard cap for one final render/quality inspection delivery. */
  renderDeadlineMs?: number;
  /** Durable lease lifetime and renewal cadence for one render attempt. */
  renderLeaseMs?: number;
  renderLeaseHeartbeatMs?: number;
  /** Clock injection makes expiry/takeover behavior deterministic in tests. */
  now?: () => Date;
  renderFailureReportDeadlineMs?: number;
  renderFailureReportWriter?: (input: {projectId: string; owner: string; error: unknown; signal: AbortSignal}) => Promise<string>;
  onProjectStatusPersisted?: (event: {projectId: string; status: ProjectStatus}) => void | Promise<void>;
};

export type RenderAttempt = {jobId: string; owner: string};
export type QualityInspection = {
  report: QualityReport;
  reportPath: string;
  previewUrl?: string;
  downloadUrl?: string;
  /** Publishes a local attempt only after its owner-token fenced DB commit succeeds. */
  publish?: () => void | Promise<void>;
};

export interface OutputQualityInspector {
  inspect(project: PipelineProject, signal?: AbortSignal, attempt?: RenderAttempt): Promise<QualityInspection>;
}

export const createFfmpegQualityInspector = (
  renderedOutput: (project: PipelineProject) => {path: string; expected: QualityExpectation}
): OutputQualityInspector => ({
  async inspect(project, signal) {
    signal?.throwIfAborted();
    const {path, expected} = renderedOutput(project);
    const report = await inspectOutput(path, expected, {signal});
    signal?.throwIfAborted();
    return {report, reportPath: qualityReportPath(), previewUrl: path, downloadUrl: path};
  }
});

export type SceneJobDelivery = {job: PipelineJob; attemptsMade: number};

export interface SceneJobConsumer {
  consume(delivery: SceneJobDelivery): Promise<void>;
}

export interface SceneJobRunner {
  run(job: PipelineJob): Promise<void>;
}

export class PipelineConfigurationError extends Error {
  readonly name = 'PipelineConfigurationError';
}

export class TransientProviderError extends Error {
  readonly name = 'TransientProviderError';

  constructor(readonly providerCause: unknown) {
    super(providerCause instanceof Error
      ? providerCause.message
      : typeof providerCause === 'string' ? providerCause : 'provider generation failed', {cause: providerCause});
  }
}

export class QualityRetryRequestedError extends Error {
  readonly name = 'QualityRetryRequestedError';

  constructor(readonly nextQualityAttempt: number) {
    super(`generated media requires quality attempt ${nextQualityAttempt}`);
  }
}

export class GeneratedMediaQualityError extends Error {
  readonly name = 'GeneratedMediaQualityError';
}

export class DeliveryAttemptsExhaustedError extends Error {
  readonly name = 'DeliveryAttemptsExhaustedError';

  constructor(readonly jobId: string) {
    super(`delivery attempts exhausted for job ${jobId}`);
  }
}

export class DeliveryClaimConflictError extends Error {
  readonly name = 'DeliveryClaimConflictError';

  constructor(readonly jobId: string) {
    super(`delivery for job ${jobId} is already claimed`);
  }
}

/** A legacy row that cannot safely enter generation or final rendering. */
export class CanonicalSceneQuarantinedError extends Error {
  readonly name = 'CanonicalSceneQuarantinedError';
  constructor(readonly sceneId: string, readonly reason: string) {
    super(`scene ${sceneId} is quarantined: ${reason}`);
  }
}

/** A persisted render failure that must not consume another BullMQ delivery. */
export class RenderJobTerminalError extends Error {
  readonly name = 'RenderJobTerminalError';

  constructor(readonly projectId: string, cause: unknown) {
    super(cause instanceof Error ? cause.message : 'project render failed', {cause});
  }
}

export class RenderDeadlineError extends Error {
  readonly name = 'RenderDeadlineError';
  constructor(readonly timeoutMs: number) { super(`project render exceeded its ${timeoutMs}ms deadline`); }
}

export class RenderLeaseLostError extends Error {
  readonly name = 'RenderLeaseLostError';
  constructor(readonly jobId: string) { super(`render lease for ${jobId} was lost`); }
}

type RenderLease = RenderAttempt & {signal: AbortSignal; now: () => Date};

const withRenderDeadline = async <T>(timeoutMs: number, parent: AbortSignal | undefined, work: (signal: AbortSignal) => Promise<T>): Promise<T> => {
  const controller = new AbortController();
  const signal = parent ? AbortSignal.any([parent, controller.signal]) : controller.signal;
  let timer: NodeJS.Timeout | undefined;
  let removeParentAbort: (() => void) | undefined;
  const parentAbort = parent && new Promise<never>((_resolve, reject) => {
    const abort = () => reject(parent.reason instanceof Error ? parent.reason : new Error('project render aborted'));
    if (parent.aborted) {
      abort();
      return;
    }
    parent.addEventListener('abort', abort, {once: true});
    removeParentAbort = () => parent.removeEventListener('abort', abort);
  });
  try {
    return await Promise.race([
      work(signal),
      ...(parentAbort ? [parentAbort] : []),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          const error = new RenderDeadlineError(timeoutMs);
          controller.abort(error);
          reject(error);
        }, timeoutMs);
      })
    ]);
  } finally {
    if (timer) clearTimeout(timer);
    removeParentAbort?.();
  }
};

export type SceneJobConsumerDependencies = Pick<PipelineDependencies,
  'repository' | 'avatarProvider' | 'imageProvider' | 'assessGeneratedMedia' | 'onRetry'>;

/**
 * Processes exactly one BullMQ delivery. BullMQ owns redelivery; this consumer
 * persists the claimed attempt before its one registry/provider dispatch.
 */
export const createSceneJobConsumer = (dependencies: SceneJobConsumerDependencies): SceneJobConsumer => {
  const generateOnce = async (job: SceneJob, kind: 'asset' | 'avatar'): Promise<void> => {
    const project = await dependencies.repository.findProject(job.data.projectId);
    const scene = (await dependencies.repository.listScenes(job.data.projectId))
      .find((candidate) => candidate.id === job.data.sceneId);
    if (!project || !scene) throw new Error(`queued scene ${job.data.sceneId} was not found`);

    if (scene.contentRevision !== job.options.contentRevision) return;
    if (!await dependencies.repository.beginSceneGeneration(project.id, scene.id, job.id, job.options.contentRevision)) return;
    let asset: GeneratedAsset;
    try {
      asset = kind === 'avatar'
        ? await dependencies.avatarProvider.generate({
          projectId: project.id, sceneId: scene.id, text: scene.script,
          avatarId: project.avatar.avatarId, voiceId: project.avatar.voiceId,
          width: 1080, height: 1920
        })
        : await dependencies.imageProvider.generate({
          projectId: project.id, sceneId: scene.id, prompt: scene.visualPrompt ?? scene.script,
          width: 1080, height: 1920
        });
    } catch (error) {
      throw new TransientProviderError(error);
    }

    const quality = dependencies.assessGeneratedMedia?.({scene, asset}) ?? {accepted: true};
    if (quality.accepted) {
      await dependencies.repository.completeSceneGeneration(project.id, scene.id, job.id, job.options.contentRevision, asset);
      return;
    }

    if (job.options.qualityAttempt >= 2) {
      const claimed = await dependencies.repository.claimJobQuality(job.id, job.options.qualityAttempt, {qualityTerminal: true});
      if (!claimed) throw new DeliveryClaimConflictError(job.id);
      throw new GeneratedMediaQualityError(quality.reason ?? 'generated media did not pass quality checks');
    }

    const nextQualityAttempt = job.options.qualityAttempt + 1;
    const claimed = await dependencies.repository.claimJobQuality(job.id, job.options.qualityAttempt, {qualityAttempt: nextQualityAttempt});
    if (!claimed) throw new DeliveryClaimConflictError(job.id);
    throw new QualityRetryRequestedError(nextQualityAttempt);
  };

  const registry = new JobNameProcessorRegistry({
    'scene.asset.generate': createAssetGenerateProcessor(generateOnce),
    'scene.avatar.generate': createAvatarGenerateProcessor(generateOnce)
  });

  return {
    async consume({job, attemptsMade}) {
      if (job.options.qualityTerminal) {
        throw new GeneratedMediaQualityError('generated media quality attempts are exhausted');
      }
      const deliveryAttempt = Math.max(job.options.deliveryAttemptsMade ?? 0, attemptsMade) + 1;
      if (deliveryAttempt > job.options.attempts) throw new DeliveryAttemptsExhaustedError(job.id);
      const claimed = await dependencies.repository.claimJobDelivery(
        job.id,
        job.options.deliveryAttemptsMade ?? 0,
        deliveryAttempt
      );
      if (!claimed) {
        const durable = await dependencies.repository.findJob(job.id);
        if ((durable.options.deliveryAttemptsMade ?? 0) >= durable.options.attempts) {
          throw new DeliveryAttemptsExhaustedError(job.id);
        }
        throw new DeliveryClaimConflictError(job.id);
      }
      try {
        await registry.dispatch(claimed);
      } catch (error) {
        if (error instanceof TransientProviderError && deliveryAttempt < job.options.attempts) {
          dependencies.onRetry?.({
            projectId: job.data.projectId,
            sceneId: job.data.sceneId,
            attempt: deliveryAttempt,
            delayMs: job.options.backoff.delay * 2 ** (deliveryAttempt - 1)
          });
        }
        throw error;
      }
    }
  };
};

/** Offline BullMQ simulator; each loop iteration is a separate delivery. */
export const createOfflineBullMqJobRunner = (
  repository: PipelineRepository,
  consumer: SceneJobConsumer
): SceneJobRunner => ({
  async run(initialJob) {
    while (true) {
      const job = await repository.findJob(initialJob.id);
      const attemptsMade = job.options.deliveryAttemptsMade ?? 0;
      try {
        await consumer.consume({job, attemptsMade});
        return;
      } catch (error) {
        if (error instanceof GeneratedMediaQualityError || error instanceof DeliveryAttemptsExhaustedError) {
          await repository.markSceneFailedAndProjectPartiallyFailed(job.data.projectId, job.data.sceneId, job.id, error.message);
          throw error;
        }
        if (error instanceof QualityRetryRequestedError) continue;
        if (error instanceof TransientProviderError) {
          const persisted = await repository.findJob(initialJob.id);
          if ((persisted.options.deliveryAttemptsMade ?? 0) >= persisted.options.attempts) {
            await repository.markSceneFailedAndProjectPartiallyFailed(job.data.projectId, job.data.sceneId, job.id, error instanceof Error ? error.message : 'scene generation failed');
            throw error.providerCause;
          }
          continue;
        }
        throw error;
      }
    }
  }
});

export class PipelineService {
  constructor(private readonly dependencies: PipelineDependencies) {}

  /** Executes a render delivery through the existing project finalization pipeline. */
  async processRenderJob(projectId: string, renderJobId: string, signal: AbortSignal): Promise<void> {
    const owner = randomUUID();
    const now = this.dependencies.now ?? (() => new Date());
    const leaseMs = this.dependencies.renderLeaseMs ?? 60_000;
    const heartbeatMs = this.dependencies.renderLeaseHeartbeatMs ?? 20_000;
    if (!Number.isFinite(leaseMs) || !Number.isFinite(heartbeatMs) || leaseMs <= heartbeatMs || heartbeatMs <= 0) {
      throw new PipelineConfigurationError('render lease duration must exceed a positive heartbeat interval');
    }
    if (!await this.dependencies.repository.claimRenderJobLease(renderJobId, owner, leaseMs)) return;
    const leaseAbort = new AbortController();
    const combinedSignal = AbortSignal.any([signal, leaseAbort.signal]);
    const leaseOperationDeadlineMs = Math.max(1, heartbeatMs - 1);
    let renewalChain = Promise.resolve();
    let renewalActive = false;
    const loseLease = (): void => {
      const leaseLoss = new RenderLeaseLostError(renderJobId);
      leaseAbort.abort(leaseLoss);
      void withRenderDeadline(leaseOperationDeadlineMs, undefined, async () => {
        await this.dependencies.repository.commitRenderJobTerminal({
          projectId, jobId: renderJobId, owner, status: 'FAILED',
          reportPath: `inline:render-lease-lost:${owner}`
        });
      }).catch(() => undefined);
    };
    const renew = async (): Promise<void> => {
      if (leaseAbort.signal.aborted) return;
      try {
        const renewed = await withRenderDeadline(leaseOperationDeadlineMs, undefined,
          async () => await this.dependencies.repository.renewRenderJobLease(renderJobId, owner, leaseMs));
        if (!renewed) loseLease();
      } catch {
        loseLease();
      }
    };
    const scheduleRenewal = (): void => {
      if (renewalActive || leaseAbort.signal.aborted) return;
      renewalActive = true;
      renewalChain = renewalChain.then(renew).finally(() => { renewalActive = false; });
    };
    const heartbeat = setInterval(scheduleRenewal, heartbeatMs);
    heartbeat.unref();
    try {
      const project = await this.dependencies.repository.findProject(projectId);
      if (!project) throw new Error(`project ${projectId} was not found`);
      if (project.status === 'COMPLETED' || project.status === 'FAILED' || project.status === 'CANCELLED') {
        const committed = await this.dependencies.repository.commitRenderJobTerminal({projectId, jobId: renderJobId, owner, status: project.status});
        if (!committed) throw new RenderLeaseLostError(renderJobId);
        return;
      }
      combinedSignal.throwIfAborted();
      await this.runProjectPipeline(projectId, combinedSignal, {jobId: renderJobId, owner, signal: combinedSignal, now});
      combinedSignal.throwIfAborted();
    } catch (error) {
      if (leaseAbort.signal.aborted && !signal.aborted) throw leaseAbort.signal.reason;
      if (signal.aborted) {
        const committed = await this.dependencies.repository.commitRenderJobTerminal({projectId, jobId: renderJobId, owner, status: 'CANCELLED'});
        if (!committed) throw new RenderLeaseLostError(renderJobId);
      }
      throw error;
    } finally {
      clearInterval(heartbeat);
      await renewalChain;
    }
  }

  private async moveProject(
    projectId: string,
    expectedStatus: ProjectStatus,
    nextStatus: ProjectStatus
  ): Promise<ProjectStatus> {
    transitionProject(expectedStatus, nextStatus);
    const claimed = await this.dependencies.repository.claimProjectStatus(projectId, expectedStatus, nextStatus);
    if (claimed) {
      await this.dependencies.onProjectStatusPersisted?.({projectId, status: nextStatus});
      return nextStatus;
    }
    const current = await this.dependencies.repository.findProject(projectId);
    if (!current) throw new Error(`project ${projectId} was not found`);
    return current.status;
  }

  private async checkOutputQuality(projectId: string, signal?: AbortSignal, lease?: RenderLease): Promise<ProjectStatus> {
    const project = await this.dependencies.repository.findProject(projectId);
    if (!project) throw new Error(`project ${projectId} was not found`);
    if (project.status !== 'QUALITY_CHECK') return project.status;
    if (!this.dependencies.qualityInspector) throw new PipelineConfigurationError('a quality inspector is required before project completion');

    let inspection: QualityInspection;
    try {
      signal?.throwIfAborted();
      inspection = await withRenderDeadline(this.dependencies.renderDeadlineMs ?? 120_000, signal,
        (inspectionSignal) => this.dependencies.qualityInspector!.inspect(project, inspectionSignal, lease && {jobId: lease.jobId, owner: lease.owner}));
      signal?.throwIfAborted();
    } catch (error) {
      signal?.throwIfAborted();
      let reportPath: string;
      try {
        reportPath = await this.writeRenderFailureReport(projectId, error, lease?.owner, signal);
      } catch (reportError) {
        const owner = lease?.owner ?? randomUUID();
        reportPath = `inline:report-write-error:${owner}:${encodeURIComponent(reportError instanceof Error ? reportError.message : String(reportError))}`;
      }
      if (lease) {
        const committed = await this.dependencies.repository.commitRenderJobTerminal({projectId, jobId: lease.jobId, owner: lease.owner, status: 'FAILED', reportPath});
        if (!committed) throw new RenderLeaseLostError(lease.jobId);
        await this.dependencies.onProjectStatusPersisted?.({projectId, status: 'FAILED'});
        throw new RenderJobTerminalError(projectId, error);
      }
      const claimedFailure = await this.dependencies.repository.claimProjectQualityResult(projectId, 'FAILED', reportPath);
      if (claimedFailure) {
        await this.dependencies.onProjectStatusPersisted?.({projectId, status: 'FAILED'});
        throw new RenderJobTerminalError(projectId, error);
      }
      const current = await this.dependencies.repository.findProject(projectId);
      if (!current) throw new Error(`project ${projectId} was not found`);
      if (current.status === 'FAILED') throw new RenderJobTerminalError(projectId, error);
      return current.status;
    }
    const nextStatus: Extract<ProjectStatus, 'COMPLETED' | 'FAILED'> = inspection.report.passed ? 'COMPLETED' : 'FAILED';
    transitionProject('QUALITY_CHECK', nextStatus);
    if (lease) {
      const committed = await this.dependencies.repository.commitRenderJobTerminal({
        projectId, jobId: lease.jobId, owner: lease.owner, status: nextStatus,
        reportPath: inspection.reportPath, output: inspection.report.passed ? {previewUrl: inspection.previewUrl, downloadUrl: inspection.downloadUrl} : undefined
      });
      if (!committed) throw new RenderLeaseLostError(lease.jobId);
      await inspection.publish?.();
      await this.dependencies.onProjectStatusPersisted?.({projectId, status: nextStatus});
      return nextStatus;
    }
    const claimed = await this.dependencies.repository.claimProjectQualityResult(projectId, nextStatus, inspection.reportPath, inspection.report.passed ? {previewUrl: inspection.previewUrl, downloadUrl: inspection.downloadUrl} : undefined);
    if (claimed) {
      await inspection.publish?.();
      await this.dependencies.onProjectStatusPersisted?.({projectId, status: nextStatus});
      return nextStatus;
    }
    const current = await this.dependencies.repository.findProject(projectId);
    if (!current) throw new Error(`project ${projectId} was not found`);
    return current.status;
  }

  private async writeRenderFailureReport(projectId: string, error: unknown, owner: string = randomUUID(), signal?: AbortSignal): Promise<string> {
    const path = resolve(process.cwd(), 'reports', projectId, owner, `render-failure-${randomUUID()}.json`);
    return await withRenderDeadline(this.dependencies.renderFailureReportDeadlineMs ?? 5_000, signal, async (reportSignal) => {
      reportSignal.throwIfAborted();
      if (this.dependencies.renderFailureReportWriter) {
        return await this.dependencies.renderFailureReportWriter({projectId, owner, error, signal: reportSignal});
      }
      await mkdir(dirname(path), {recursive: true});
      reportSignal.throwIfAborted();
      await writeFile(path, `${JSON.stringify({passed: false, errors: [error instanceof Error ? error.message : String(error)], metrics: {}}, null, 2)}\n`, 'utf8');
      reportSignal.throwIfAborted();
      return path;
    });
  }

  async runProjectPipeline(projectId: string, signal?: AbortSignal, lease?: RenderLease): Promise<void> {
    signal?.throwIfAborted();
    const project = await this.dependencies.repository.findProject(projectId);
    if (!project) throw new Error(`project ${projectId} was not found`);
    signal?.throwIfAborted();

    let status: ProjectStatus = project.status;
    const move = async (next: ProjectStatus): Promise<void> => {
      status = await this.moveProject(projectId, status, next);
    };

    if (status === 'ALIGNING_TIMELINE') await move('RENDERING');
    if (status === 'RENDERING') await move('QUALITY_CHECK');
    if (status === 'QUALITY_CHECK') {
      await this.checkOutputQuality(projectId, signal, lease);
      return;
    }
    if (status === 'COMPLETED' || status === 'CANCELLED') return;
    if (status === 'CREATED' || status === 'NEEDS_USER_INPUT' || status === 'STORYBOARDING') {
      if (status !== 'STORYBOARDING') await move('STORYBOARDING');
      if (status === 'STORYBOARDING') {
        const board = buildProjectStoryboard(this.dependencies.buildStoryboard, project);
        await this.dependencies.repository.persistScenes(projectId, board.scenes);
      }
    }
    if (status === 'FAILED') await move('RETRYING');
    if (status === 'STORYBOARDING' || status === 'RETRYING' || status === 'PARTIALLY_FAILED') await move('GENERATING_ASSETS');
    if (status !== 'GENERATING_ASSETS' && status !== 'GENERATING_AVATAR') return;
    const scenes = await this.dependencies.repository.listScenes(projectId);

    const results = await Promise.allSettled(scenes
      .filter((scene) => scene.status !== 'READY' && scene.status !== 'FALLBACK_ACCEPTED')
      .map((scene) => this.generateScene(project, scene)));
    status = (await this.dependencies.repository.findProject(projectId))?.status ?? status;
    if (results.some((result) => result.status === 'fulfilled' && result.value === 'NEEDS_USER_INPUT')) {
      if (status === 'GENERATING_ASSETS' || status === 'GENERATING_AVATAR' || status === 'PARTIALLY_FAILED') {
        await move('NEEDS_USER_INPUT');
      }
      return;
    }
    if (results.some((result) => result.status === 'rejected')) {
      if (status === 'GENERATING_ASSETS' || status === 'GENERATING_AVATAR') await move('PARTIALLY_FAILED');
      throw (results.find((result) => result.status === 'rejected') as PromiseRejectedResult).reason;
    }
    if (results.some((result) => result.status === 'fulfilled' && result.value === 'QUEUED')) return;
    signal?.throwIfAborted();
    await this.checkProjectFanIn(projectId, signal);
  }

  /** Scene workers call this after persisting a usable result. */
  async checkProjectFanIn(projectId: string, signal?: AbortSignal): Promise<boolean> {
    signal?.throwIfAborted();
    const project = await this.dependencies.repository.findProject(projectId);
    if (!project) throw new Error(`project ${projectId} was not found`);
    let status: ProjectStatus = project.status;
    if (status === 'COMPLETED') return true;
    const scenes = await this.dependencies.repository.listScenes(projectId);
    if (!allRequiredScenesReady(scenes)) return false;

    const move = async (next: ProjectStatus): Promise<void> => {
      status = await this.moveProject(projectId, status, next);
    };
    if (status === 'GENERATING_ASSETS' || status === 'GENERATING_AVATAR') await move('ALIGNING_TIMELINE');
    if (status === 'ALIGNING_TIMELINE') await move('RENDERING');
    if (status === 'RENDERING') await move('QUALITY_CHECK');
    if (status === 'QUALITY_CHECK') return (await this.checkOutputQuality(projectId, signal)) === 'COMPLETED';
    return (await this.dependencies.repository.findProject(projectId))?.status === 'COMPLETED';
  }

  /** Scene workers call this before acknowledging a terminal delivery failure. */
  async markSceneTerminalFailure(projectId: string, sceneId: string, generationJobId: string, failureReason = 'scene generation failed'): Promise<void> {
    const result = await this.dependencies.repository.markSceneFailedAndProjectPartiallyFailed(projectId, sceneId, generationJobId, failureReason);
    if (result?.transitioned) {
      await this.dependencies.onProjectStatusPersisted?.({projectId, status: 'PARTIALLY_FAILED'});
    }
  }

  /** Converts an API regeneration control job into a fresh typed generation job. */
  async regenerateScene(
    projectId: string,
    sceneId: string,
    generationRequestId: string,
    expectedGenerationJobId: string | undefined
  ): Promise<void> {
    const project = await this.dependencies.repository.findProject(projectId);
    if (!project) throw new Error(`project ${projectId} was not found`);
    const scene = (await this.dependencies.repository.listScenes(projectId))
      .find((candidate) => candidate.id === sceneId);
    if (!scene) throw new Error(`scene ${sceneId} was not found in project ${projectId}`);

    const usesSuppliedFactualAsset = scene.asset?.factual === true &&
      (scene.type === 'screenshot' || scene.type === 'upload');
    const generationJob = usesSuppliedFactualAsset
      ? undefined
      : this.createSceneGenerationJob(project, scene, generationRequestId);
    let status = project.status;
    if (generationJob) {
      const nextProjectStatus: RegenerationProjectStatus = generationJob.name === 'scene.avatar.generate'
        ? 'GENERATING_AVATAR'
        : 'GENERATING_ASSETS';
      const activation = await this.dependencies.repository.reserveAndActivateSceneRegeneration(
        generationJob, expectedGenerationJobId, scene.contentRevision, nextProjectStatus
      );
      if (activation) {
        status = activation.project.status;
        if (activation.transitioned) {
          await this.dependencies.onProjectStatusPersisted?.({projectId, status: nextProjectStatus});
        }
      } else {
        const current = (await this.dependencies.repository.listScenes(projectId))
          .find((candidate) => candidate.id === sceneId);
        if (current?.activeGenerationJobId !== generationJob.id) return;
        status = (await this.dependencies.repository.findProject(projectId))?.status ?? status;
      }
    }
    const result = await this.generateScene(project, scene, generationRequestId, generationJob, generationJob !== undefined);
    if (result === 'NEEDS_USER_INPUT') {
      if (status === 'GENERATING_ASSETS' || status === 'GENERATING_AVATAR' || status === 'PARTIALLY_FAILED') {
        await this.moveProject(projectId, status, 'NEEDS_USER_INPUT');
      }
      return;
    }
    if (result === 'READY') await this.checkProjectFanIn(projectId);
  }

  private async generateScene(
    project: PipelineProject,
    scene: PipelineScene,
    generationRequestId?: string,
    preparedJob?: PipelineJob,
    activeGenerationClaimed = false
  ): Promise<'READY' | 'NEEDS_USER_INPUT' | 'QUEUED'> {
    if (isMissingFactualAsset(scene)) {
      await this.dependencies.repository.updateSceneStatus(project.id, scene.id, 'NEEDS_USER_INPUT');
      return 'NEEDS_USER_INPUT';
    }
    if (scene.asset?.factual === true && (scene.type === 'screenshot' || scene.type === 'upload')) {
      const suppliedAsset = scene.assetVersions.at(-1);
      if (!suppliedAsset) throw new Error(`factual scene ${scene.id} has no supplied asset`);
      assertAllowedProvenance(scene, suppliedAsset);
      await this.dependencies.repository.updateSceneStatus(project.id, scene.id, 'READY');
      return 'READY';
    }
    const job = preparedJob ?? this.createSceneGenerationJob(project, scene, generationRequestId);
    if (!activeGenerationClaimed) {
      if (scene.activeGenerationJobId !== undefined && scene.activeGenerationJobId !== job.id) return 'QUEUED';
      if (scene.activeGenerationJobId === undefined) {
        const claimed = await this.dependencies.repository.claimSceneActiveGenerationJob(
          project.id, scene.id, undefined, job.id, scene.contentRevision
        );
        if (!claimed) {
          const current = (await this.dependencies.repository.listScenes(project.id))
            .find((candidate) => candidate.id === scene.id);
          if (current?.activeGenerationJobId !== job.id) return 'QUEUED';
        }
      }
    }
    const reserved = await this.dependencies.repository.reserveJob(job);
    if (reserved.job.status !== 'QUEUED') {
      await this.dependencies.queue.submit(reserved.job);
      const queued = await this.dependencies.repository.markJobQueued(reserved.job.id);
      if (!queued && reserved.job.status === 'PENDING') throw new Error(`job ${reserved.job.id} could not be queued from PENDING`);
    }

    if (!this.dependencies.jobRunner) {
      if (this.dependencies.deferSceneJobs) return 'QUEUED';
      throw new PipelineConfigurationError('a registered scene job runner is required');
    }
    await this.dependencies.jobRunner.run(reserved.job);
    const processed = (await this.dependencies.repository.listScenes(project.id)).find((candidate) => candidate.id === scene.id);
    if (processed?.status === 'NEEDS_USER_INPUT') return 'NEEDS_USER_INPUT';
    if (processed?.status === 'READY' || processed?.status === 'FALLBACK_ACCEPTED') return 'READY';
    throw new Error(`processor did not complete scene ${scene.id}`);
  }

  private createSceneGenerationJob(
    project: PipelineProject,
    scene: PipelineScene,
    generationRequestId?: string
  ): PipelineJob {
    const taskType = isAvatarScene(scene) ? 'scene.avatar.generate' : 'scene.asset.generate';
    return {
      id: jobKey(project.id, scene.id, taskType, canonicalInputHash({
        taskType,
        scene: {
          id: scene.id,
          type: scene.type,
          script: scene.script,
          ...(scene.visualPrompt === undefined ? {} : {visualPrompt: scene.visualPrompt}),
          contentRevision: scene.contentRevision,
          asset: scene.asset ?? null
        },
        ...(generationRequestId ? {generationRequestId} : {})
      })),
      name: taskType,
      data: {projectId: project.id, sceneId: scene.id},
      options: {attempts: 3, backoff: {type: 'exponential', delay: 100}, qualityAttempt: 1, contentRevision: scene.contentRevision},
      status: 'PENDING'
    };
  }
}

export const runProjectPipeline = (dependencies: PipelineDependencies, projectId: string): Promise<void> =>
  new PipelineService(dependencies).runProjectPipeline(projectId);
