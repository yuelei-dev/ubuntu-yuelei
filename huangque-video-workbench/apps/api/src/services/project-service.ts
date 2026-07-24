import {createHash} from 'node:crypto';
import type {ProjectStatus} from '@huangque/contracts';
import type {EditableScenePatch} from '@huangque/contracts';
import type {QueueAdapter, QueueSubmission} from '../queue.js';
import {jobKey} from '../queue.js';

export type ProjectInput = {
  input: {type: 'script'; content: string};
  avatar: {avatarId: string; voiceId: string};
  output: {templateId: string};
};

export type ProjectRecord = ProjectInput & {
  id: string;
  ownerUsername: string;
  title: string;
  status: ProjectStatus;
  qualityReportPath?: string;
  previewUrl?: string;
  downloadUrl?: string;
  createdAt: string;
  updatedAt: string;
};

export type SceneRecord = {
  id: string;
  projectId: string;
  order: number;
  status: string;
  script: string;
  visual: Record<string, unknown>;
  asset?: Record<string, unknown> | null;
  failureReason?: string;
  createdAt: string;
  updatedAt: string;
};

export type AssetVersionRecord = {
  id: string;
  projectId: string;
  sceneId: string;
  version: number;
  uri: string;
  provenance: 'uploaded' | 'enterprise' | 'licensed' | 'generated' | 'fallback';
  inputHash: string;
  createdAt: string;
};

export type JobRecord = {
  id: string;
  projectId: string;
  sceneId: string;
  taskType: string;
  inputHash: string;
  dispatchPayload?: unknown;
  status: 'PENDING' | 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
  renderLeaseOwner?: string;
  renderLeaseExpiresAt?: string;
  dispatchAttempts?: number;
  nextDispatchAt?: string;
  dispatchLeaseOwner?: string;
  dispatchLeaseExpiresAt?: string;
  options?: {
    attempts: number;
    backoff: {type: 'exponential'; delay: number};
    qualityAttempt?: number;
    contentRevision?: number;
    deliveryAttemptsMade?: number;
    qualityTerminal?: boolean;
    outboxQuarantineReason?: string;
  };
  createdAt: string;
};

export type ProjectDetail = ProjectRecord & {scenes: SceneRecord[]; jobs: JobRecord[]; assetVersions: AssetVersionRecord[]};
export type ScenePatch = Partial<Pick<SceneRecord, 'status' | 'script' | 'visual' | 'asset' | 'failureReason'>>;
export type AssetVersionWrite = Pick<AssetVersionRecord, 'uri' | 'provenance' | 'inputHash' | 'createdAt'>;
export type JobGenerationStatePatch = Partial<Pick<NonNullable<JobRecord['options']>, 'qualityAttempt' | 'deliveryAttemptsMade' | 'qualityTerminal'>>;
export type JobQualityClaimPatch = Partial<Pick<NonNullable<JobRecord['options']>, 'qualityAttempt' | 'qualityTerminal'>>;
export type TerminalProjectFailureResult = {project: ProjectRecord; transitioned: boolean; sceneUpdated: boolean};
export type RenderJobTerminalCommit = {
  projectId: string;
  jobId: string;
  owner: string;
  status: Extract<JobRecord['status'], 'COMPLETED' | 'FAILED' | 'CANCELLED'>;
  reportPath?: string;
  output?: {previewUrl?: string; downloadUrl?: string};
};
export type RenderJobTerminalCommitResult = {project: ProjectRecord; job: JobRecord};
export type RegenerationProjectStatus = Extract<ProjectStatus, 'GENERATING_ASSETS' | 'GENERATING_AVATAR'>;
export type SceneRegenerationActivationResult = {project: ProjectRecord; scene: SceneRecord; transitioned: boolean};
export type SceneRegenerationJobActivationResult = SceneRegenerationActivationResult & {job: JobRecord; existing: boolean};
export const TERMINAL_FAILURE_TRANSITION_SOURCES = ['GENERATING_ASSETS', 'GENERATING_AVATAR'] as const satisfies readonly ProjectStatus[];
export const REGENERATION_ACTIVATION_PROJECT_SOURCES = [
  'GENERATING_ASSETS', 'GENERATING_AVATAR', 'ALIGNING_TIMELINE', 'RENDERING', 'QUALITY_CHECK',
  'RETRYING', 'PARTIALLY_FAILED', 'COMPLETED'
] as const satisfies readonly ProjectStatus[];
export const REGENERATION_ACTIVATION_SCENE_SOURCES = [
  'PENDING', 'GENERATING', 'READY', 'FALLBACK_ACCEPTED', 'FAILED'
] as const;

const canTransitionToPartialFailure = (status: ProjectStatus): boolean =>
  (TERMINAL_FAILURE_TRANSITION_SOURCES as readonly ProjectStatus[]).includes(status);

const preservesProjectWhileFailingScene = (status: ProjectStatus): boolean =>
  status === 'NEEDS_USER_INPUT' || status === 'PARTIALLY_FAILED';

export interface ProjectRepository {
  transaction<T>(work: () => Promise<T>): Promise<T>;
  createProject(project: ProjectRecord): Promise<void>;
  createProjectWithInitialJobQuota?(
    project: ProjectRecord, job: JobRecord, maxActiveProjects: number
  ): Promise<boolean>;
  findProject(id: string, ownerUsername: string): Promise<ProjectDetail | undefined>;
  findProjectForWorker(id: string): Promise<ProjectDetail | undefined>;
  updateProjectStatus(projectId: string, status: ProjectStatus): Promise<void>;
  claimProjectStatus(projectId: string, expectedStatus: ProjectStatus, nextStatus: ProjectStatus): Promise<ProjectRecord | undefined>;
  claimProjectQualityResult(projectId: string, nextStatus: Extract<ProjectStatus, 'COMPLETED' | 'FAILED'>, qualityReportPath: string, output?: {previewUrl?: string; downloadUrl?: string}): Promise<ProjectRecord | undefined>;
  updateProjectOutput(projectId: string, output: {previewUrl?: string; downloadUrl?: string}): Promise<ProjectRecord | undefined>;
  markSceneFailedAndProjectPartiallyFailed(projectId: string, sceneId: string, expectedGenerationJobId: string, failureReason?: string): Promise<TerminalProjectFailureResult | undefined>;
  createScene(scene: SceneRecord): Promise<void>;
  activateSceneRegeneration(projectId: string, sceneId: string, expectedGenerationJobId: string | undefined, nextGenerationJobId: string, expectedContentRevision: number, nextProjectStatus: RegenerationProjectStatus): Promise<SceneRegenerationActivationResult | undefined>;
  reserveAndActivateSceneRegeneration(job: JobRecord, expectedGenerationJobId: string | undefined, expectedContentRevision: number, nextProjectStatus: RegenerationProjectStatus): Promise<SceneRegenerationJobActivationResult | undefined>;
  claimSceneActiveGenerationJob(projectId: string, sceneId: string, expectedGenerationJobId: string | undefined, nextGenerationJobId: string, expectedContentRevision: number): Promise<SceneRecord | undefined>;
  beginSceneGeneration(projectId: string, sceneId: string, generationJobId: string, expectedContentRevision: number): Promise<SceneRecord | undefined>;
  completeSceneGeneration(projectId: string, sceneId: string, generationJobId: string, expectedContentRevision: number, asset: AssetVersionWrite): Promise<AssetVersionRecord | undefined>;
  updateScene(projectId: string, sceneId: string, patch: EditableScenePatch, ownerUsername: string): Promise<SceneRecord | undefined>;
  updateSceneForWorker(projectId: string, sceneId: string, patch: ScenePatch): Promise<SceneRecord | undefined>;
  reserveJob(job: JobRecord): Promise<{job: JobRecord; existing: boolean}>;
  findJob(id: string): Promise<JobRecord | undefined>;
  claimJobDelivery(id: string, expectedDeliveryAttemptsMade: number, nextDeliveryAttemptsMade: number): Promise<JobRecord | undefined>;
  claimJobQuality(id: string, expectedQualityAttempt: number, patch: JobQualityClaimPatch): Promise<JobRecord | undefined>;
  markJobQueued(id: string): Promise<JobRecord | undefined>;
  claimDueJobs(owner: string, limit: number, leaseDurationMs: number): Promise<JobRecord[]>;
  markJobDispatched(id: string, owner: string): Promise<JobRecord | undefined>;
  recordJobDispatchFailure(id: string, owner: string, nextDispatchAt: Date): Promise<JobRecord | undefined>;
  updateJobQualityAttempt(id: string, qualityAttempt: number): Promise<void>;
  updateJobGenerationState(id: string, patch: JobGenerationStatePatch): Promise<void>;
  claimRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<JobRecord | undefined>;
  renewRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<JobRecord | undefined>;
  commitRenderJobTerminal(commit: RenderJobTerminalCommit): Promise<RenderJobTerminalCommitResult | undefined>;
  listAssetVersions(projectId: string, sceneId: string): Promise<AssetVersionRecord[]>;
  createAssetVersion(asset: AssetVersionRecord): Promise<void>;
}

const canonicalJson = (value: unknown): string => {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('job input contains a non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left === right ? 0 : left < right ? -1 : 1)
      .map(([key, entry]) => `${JSON.stringify(key)}:${canonicalJson(entry)}`)
      .join(',')}}`;
  }
  throw new Error('job input contains an unsupported value');
};

export const canonicalInputHash = (input: unknown): string => createHash('sha256').update(canonicalJson(input)).digest('hex');

const clone = <T>(value: T): T => structuredClone(value);

export class InMemoryProjectRepository implements ProjectRepository {
  private readonly projectRecords = new Map<string, ProjectRecord>();
  private readonly sceneRecords = new Map<string, SceneRecord>();
  private readonly jobRecords = new Map<string, JobRecord>();
  private readonly assetVersionRecords = new Map<string, AssetVersionRecord>();
  constructor(private readonly now: () => Date = () => new Date()) {}

  async transaction<T>(work: () => Promise<T>): Promise<T> {
    const projectSnapshot = new Map([...this.projectRecords].map(([key, value]) => [key, clone(value)]));
    const sceneSnapshot = new Map([...this.sceneRecords].map(([key, value]) => [key, clone(value)]));
    const jobSnapshot = new Map([...this.jobRecords].map(([key, value]) => [key, clone(value)]));
    const assetSnapshot = new Map([...this.assetVersionRecords].map(([key, value]) => [key, clone(value)]));
    try {
      return await work();
    } catch (error) {
      this.projectRecords.clear();
      projectSnapshot.forEach((value, key) => this.projectRecords.set(key, value));
      this.sceneRecords.clear();
      sceneSnapshot.forEach((value, key) => this.sceneRecords.set(key, value));
      this.jobRecords.clear();
      jobSnapshot.forEach((value, key) => this.jobRecords.set(key, value));
      this.assetVersionRecords.clear();
      assetSnapshot.forEach((value, key) => this.assetVersionRecords.set(key, value));
      throw error;
    }
  }

  async createProject(project: ProjectRecord): Promise<void> {
    this.projectRecords.set(project.id, clone(project));
  }

  async createProjectWithInitialJobQuota(
    project: ProjectRecord,
    job: JobRecord,
    maxActiveProjects: number
  ): Promise<boolean> {
    const terminal = new Set<ProjectStatus>(['COMPLETED', 'FAILED', 'CANCELLED']);
    const active = [...this.projectRecords.values()]
      .filter((candidate) => candidate.ownerUsername === project.ownerUsername && !terminal.has(candidate.status))
      .length;
    if (active >= maxActiveProjects) return false;
    // No await occurs between the quota decision and both writes.
    this.projectRecords.set(project.id, clone(project));
    this.jobRecords.set(job.id, clone(job));
    return true;
  }

  async findProject(id: string, ownerUsername: string): Promise<ProjectDetail | undefined> {
    const project = this.projectRecords.get(id);
    if (!project || project.ownerUsername !== ownerUsername) return undefined;
    return this.projectDetail(project);
  }

  async findProjectForWorker(id: string): Promise<ProjectDetail | undefined> {
    const project = this.projectRecords.get(id);
    return project && this.projectDetail(project);
  }

  private projectDetail(project: ProjectRecord): ProjectDetail {
    return {
      ...clone(project),
      scenes: [...this.sceneRecords.values()].filter((scene) => scene.projectId === project.id).sort((a, b) => a.order - b.order).map(clone),
      jobs: [...this.jobRecords.values()].filter((job) => job.projectId === project.id).map(clone),
      assetVersions: [...this.assetVersionRecords.values()].filter((asset) => asset.projectId === project.id).map(clone)
    };
  }

  async updateProjectStatus(projectId: string, status: ProjectStatus): Promise<void> {
    const project = this.projectRecords.get(projectId);
    if (!project) throw new Error(`project ${projectId} was not found`);
    this.projectRecords.set(projectId, {...project, status, updatedAt: new Date().toISOString()});
  }

  async claimProjectStatus(
    projectId: string,
    expectedStatus: ProjectStatus,
    nextStatus: ProjectStatus
  ): Promise<ProjectRecord | undefined> {
    const project = this.projectRecords.get(projectId);
    if (!project || project.status !== expectedStatus) return undefined;
    const claimed = {...project, status: nextStatus, updatedAt: new Date().toISOString()};
    this.projectRecords.set(projectId, claimed);
    return clone(claimed);
  }

  async claimProjectQualityResult(
    projectId: string,
    nextStatus: Extract<ProjectStatus, 'COMPLETED' | 'FAILED'>,
    qualityReportPath: string,
    output?: {previewUrl?: string; downloadUrl?: string}
  ): Promise<ProjectRecord | undefined> {
    const project = this.projectRecords.get(projectId);
    if (!project || project.status !== 'QUALITY_CHECK') return undefined;
    const claimed = {...project, status: nextStatus, qualityReportPath, ...output, updatedAt: new Date().toISOString()};
    this.projectRecords.set(projectId, claimed);
    return clone(claimed);
  }

  async updateProjectOutput(projectId: string, output: {previewUrl?: string; downloadUrl?: string}): Promise<ProjectRecord | undefined> {
    const project = this.projectRecords.get(projectId); if (!project) return undefined;
    const updated = {...project, ...output, updatedAt: new Date().toISOString()}; this.projectRecords.set(projectId, updated); return clone(updated);
  }

  async markSceneFailedAndProjectPartiallyFailed(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string, failureReason?: string
  ): Promise<TerminalProjectFailureResult | undefined> {
    const project = this.projectRecords.get(projectId);
    const sceneKey = `${projectId}:${sceneId}`;
    const scene = this.sceneRecords.get(sceneKey);
    if (!project || !scene) return undefined;
    if (scene.visual.activeGenerationJobId !== expectedGenerationJobId || scene.status !== 'GENERATING') {
      return {project: clone(project), transitioned: false, sceneUpdated: false};
    }
    const transitionsProject = canTransitionToPartialFailure(project.status);
    if (!transitionsProject && !preservesProjectWhileFailingScene(project.status)) {
      return {project: clone(project), transitioned: false, sceneUpdated: false};
    }
    const now = new Date().toISOString();
    const failedProject = transitionsProject
      ? {...project, status: 'PARTIALLY_FAILED' as const, updatedAt: now}
      : project;
    this.projectRecords.set(projectId, failedProject);
    this.sceneRecords.set(sceneKey, {...scene, status: 'FAILED', ...(failureReason ? {failureReason} : {}), updatedAt: now});
    return {project: clone(failedProject), transitioned: transitionsProject, sceneUpdated: true};
  }

  async createScene(scene: SceneRecord): Promise<void> {
    this.sceneRecords.set(`${scene.projectId}:${scene.id}`, clone(scene));
  }

  async activateSceneRegeneration(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string | undefined,
    nextGenerationJobId: string,
    expectedContentRevision: number,
    nextProjectStatus: RegenerationProjectStatus
  ): Promise<SceneRegenerationActivationResult | undefined> {
    const project = this.projectRecords.get(projectId);
    const sceneKey = `${projectId}:${sceneId}`;
    const scene = this.sceneRecords.get(sceneKey);
    if (!project || !scene ||
        !(REGENERATION_ACTIVATION_PROJECT_SOURCES as readonly ProjectStatus[]).includes(project.status) ||
        !(REGENERATION_ACTIVATION_SCENE_SOURCES as readonly string[]).includes(scene.status) ||
        scene.visual.activeGenerationJobId !== expectedGenerationJobId ||
        (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0) !== expectedContentRevision) return undefined;
    const now = new Date().toISOString();
    const activatedProject = {...project, status: nextProjectStatus, updatedAt: now};
    const {failureReason: _failureReason, ...sceneWithoutFailure} = scene;
    const activatedScene = {
      ...sceneWithoutFailure,
      status: 'PENDING',
      visual: {...scene.visual, activeGenerationJobId: nextGenerationJobId, activeGenerationContentRevision: expectedContentRevision},
      updatedAt: now
    };
    this.projectRecords.set(projectId, activatedProject);
    this.sceneRecords.set(sceneKey, activatedScene);
    return {
      project: clone(activatedProject),
      scene: clone(activatedScene),
      transitioned: project.status !== nextProjectStatus
    };
  }

  async reserveAndActivateSceneRegeneration(
    job: JobRecord,
    expectedGenerationJobId: string | undefined,
    expectedContentRevision: number,
    nextProjectStatus: RegenerationProjectStatus
  ): Promise<SceneRegenerationJobActivationResult | undefined> {
    const project = this.projectRecords.get(job.projectId);
    const sceneKey = `${job.projectId}:${job.sceneId}`;
    const scene = this.sceneRecords.get(sceneKey);
    if (!project || !scene ||
        !(REGENERATION_ACTIVATION_PROJECT_SOURCES as readonly ProjectStatus[]).includes(project.status) ||
        !(REGENERATION_ACTIVATION_SCENE_SOURCES as readonly string[]).includes(scene.status) ||
        scene.visual.activeGenerationJobId !== expectedGenerationJobId ||
        (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0) !== expectedContentRevision) return undefined;
    const existing = this.jobRecords.get(job.id);
    const durableJob = existing ?? clone(job);
    if (!existing) this.jobRecords.set(job.id, durableJob);
    const now = this.now().toISOString();
    const activatedProject = {...project, status: nextProjectStatus, updatedAt: now};
    const {failureReason: _failureReason, ...sceneWithoutFailure} = scene;
    const activatedScene = {
      ...sceneWithoutFailure,
      status: 'PENDING',
      visual: {...scene.visual, activeGenerationJobId: job.id, activeGenerationContentRevision: expectedContentRevision},
      updatedAt: now
    };
    this.projectRecords.set(project.id, activatedProject);
    this.sceneRecords.set(sceneKey, activatedScene);
    return {
      project: clone(activatedProject),
      scene: clone(activatedScene),
      job: clone(durableJob),
      existing: Boolean(existing),
      transitioned: project.status !== nextProjectStatus
    };
  }

  async claimSceneActiveGenerationJob(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string | undefined,
    nextGenerationJobId: string,
    expectedContentRevision: number
  ): Promise<SceneRecord | undefined> {
    const key = `${projectId}:${sceneId}`;
    const scene = this.sceneRecords.get(key);
    if (!scene || scene.visual.activeGenerationJobId !== expectedGenerationJobId ||
        (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0) !== expectedContentRevision) return undefined;
    const updated = {
      ...scene,
      visual: {...scene.visual, activeGenerationJobId: nextGenerationJobId, activeGenerationContentRevision: expectedContentRevision},
      updatedAt: new Date().toISOString()
    };
    this.sceneRecords.set(key, updated);
    return clone(updated);
  }

  async beginSceneGeneration(
    projectId: string,
    sceneId: string,
    generationJobId: string,
    expectedContentRevision: number
  ): Promise<SceneRecord | undefined> {
    const key = `${projectId}:${sceneId}`;
    const scene = this.sceneRecords.get(key);
    if (!scene || scene.visual.activeGenerationJobId !== generationJobId ||
        (typeof scene.visual.activeGenerationContentRevision === 'number'
          ? scene.visual.activeGenerationContentRevision
          : (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0)) !== expectedContentRevision ||
        (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0) !== expectedContentRevision ||
        !['PENDING', 'GENERATING', 'NEEDS_USER_INPUT', 'FAILED'].includes(scene.status)) return undefined;
    const updated = {...scene, status: 'GENERATING', updatedAt: new Date().toISOString()};
    this.sceneRecords.set(key, updated);
    return clone(updated);
  }

  async completeSceneGeneration(
    projectId: string,
    sceneId: string,
    generationJobId: string,
    expectedContentRevision: number,
    asset: AssetVersionWrite
  ): Promise<AssetVersionRecord | undefined> {
    const key = `${projectId}:${sceneId}`;
    const scene = this.sceneRecords.get(key);
    if (!scene || scene.visual.activeGenerationJobId !== generationJobId ||
        (typeof scene.visual.activeGenerationContentRevision === 'number'
          ? scene.visual.activeGenerationContentRevision
          : (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0)) !== expectedContentRevision ||
        (typeof scene.visual.contentRevision === 'number' ? scene.visual.contentRevision : 0) !== expectedContentRevision ||
        scene.status !== 'GENERATING') return undefined;
    const versions = [...this.assetVersionRecords.values()]
      .filter((candidate) => candidate.projectId === projectId && candidate.sceneId === sceneId);
    const version = Math.max(0, ...versions.map((candidate) => candidate.version)) + 1;
    const record: AssetVersionRecord = {
      ...clone(asset), id: `${projectId}:${sceneId}:v${version}`, projectId, sceneId, version
    };
    this.assetVersionRecords.set(record.id, record);
    this.sceneRecords.set(key, {...scene, status: 'READY', updatedAt: new Date().toISOString()});
    return clone(record);
  }

  async updateScene(projectId: string, sceneId: string, patch: EditableScenePatch, ownerUsername: string): Promise<SceneRecord | undefined> {
    if (this.projectRecords.get(projectId)?.ownerUsername !== ownerUsername) return undefined;
    const key = `${projectId}:${sceneId}`;
    const existing = this.sceneRecords.get(key);
    if (!existing) return undefined;
    const updated: SceneRecord = {
      ...existing,
      ...(patch.script === undefined ? {} : {script: patch.script}),
      visual: {
        ...existing.visual,
        ...(patch.visual ?? {}),
        ...(patch.visualPrompt === undefined ? {} : {visualPrompt: patch.visualPrompt}),
        contentRevision: (typeof existing.visual.contentRevision === 'number' ? existing.visual.contentRevision : 0) + 1
      },
      updatedAt: new Date().toISOString()
    };
    this.sceneRecords.set(key, updated);
    return clone(updated);
  }

  async updateSceneForWorker(projectId: string, sceneId: string, patch: ScenePatch): Promise<SceneRecord | undefined> {
    const key = `${projectId}:${sceneId}`;
    const existing = this.sceneRecords.get(key);
    if (!existing) return undefined;
    const updated = {...existing, ...clone(patch), updatedAt: new Date().toISOString()};
    this.sceneRecords.set(key, updated);
    return clone(updated);
  }

  async reserveJob(job: JobRecord): Promise<{job: JobRecord; existing: boolean}> {
    const existing = this.jobRecords.get(job.id);
    if (existing) return {job: clone(existing), existing: true};
    this.jobRecords.set(job.id, clone(job));
    return {job: clone(job), existing: false};
  }

  async findJob(id: string): Promise<JobRecord | undefined> {
    const job = this.jobRecords.get(id);
    return job && clone(job);
  }

  async claimJobDelivery(id: string, expectedDeliveryAttemptsMade: number, nextDeliveryAttemptsMade: number): Promise<JobRecord | undefined> {
    const job = this.jobRecords.get(id);
    if (!job?.options || job.options.qualityTerminal === true) return undefined;
    if ((job.options.deliveryAttemptsMade ?? 0) !== expectedDeliveryAttemptsMade) return undefined;
    const claimed = {...job, options: {...job.options, deliveryAttemptsMade: nextDeliveryAttemptsMade}};
    this.jobRecords.set(id, claimed);
    return clone(claimed);
  }

  async claimJobQuality(id: string, expectedQualityAttempt: number, patch: JobQualityClaimPatch): Promise<JobRecord | undefined> {
    const job = this.jobRecords.get(id);
    if (!job?.options || job.options.qualityTerminal === true) return undefined;
    if ((job.options.qualityAttempt ?? 1) !== expectedQualityAttempt) return undefined;
    const claimed = {...job, options: {...job.options, ...patch}};
    this.jobRecords.set(id, claimed);
    return clone(claimed);
  }

  async markJobQueued(id: string): Promise<JobRecord | undefined> {
    const job = this.jobRecords.get(id);
    if (!job) throw new Error(`job ${id} was not reserved`);
    if (job.status !== 'PENDING') return undefined;
    const queued = {...job, status: 'QUEUED' as const};
    this.jobRecords.set(id, queued);
    return clone(queued);
  }

  async claimDueJobs(owner: string, limit: number, leaseDurationMs: number): Promise<JobRecord[]> {
    const now = this.now();
    const claimed: JobRecord[] = [];
    for (const job of this.jobRecords.values()) {
      if (claimed.length >= limit || job.status !== 'PENDING') continue;
      if (job.dispatchPayload == null) continue;
      if (job.nextDispatchAt && new Date(job.nextDispatchAt) > now) continue;
      if (job.dispatchLeaseExpiresAt && new Date(job.dispatchLeaseExpiresAt) > now) continue;
      const updated = {
        ...job,
        dispatchLeaseOwner: owner,
        dispatchLeaseExpiresAt: new Date(now.getTime() + leaseDurationMs).toISOString()
      };
      this.jobRecords.set(job.id, updated);
      claimed.push(clone(updated));
    }
    return claimed;
  }

  async markJobDispatched(id: string, owner: string): Promise<JobRecord | undefined> {
    const job = this.jobRecords.get(id);
    if (!job || job.status !== 'PENDING' || job.dispatchLeaseOwner !== owner) return undefined;
    const updated = {...job, status: 'QUEUED' as const, dispatchLeaseOwner: undefined, dispatchLeaseExpiresAt: undefined};
    this.jobRecords.set(id, updated);
    return clone(updated);
  }

  async recordJobDispatchFailure(id: string, owner: string, nextDispatchAt: Date): Promise<JobRecord | undefined> {
    const job = this.jobRecords.get(id);
    if (!job || job.status !== 'PENDING' || job.dispatchLeaseOwner !== owner) return undefined;
    const updated = {
      ...job,
      dispatchAttempts: (job.dispatchAttempts ?? 0) + 1,
      nextDispatchAt: nextDispatchAt.toISOString(),
      dispatchLeaseOwner: undefined,
      dispatchLeaseExpiresAt: undefined
    };
    this.jobRecords.set(id, updated);
    return clone(updated);
  }

  async updateJobQualityAttempt(id: string, qualityAttempt: number): Promise<void> {
    await this.updateJobGenerationState(id, {qualityAttempt});
  }

  async updateJobGenerationState(id: string, patch: JobGenerationStatePatch): Promise<void> {
    const job = this.jobRecords.get(id);
    if (!job?.options) throw new Error(`job ${id} has no generation options`);
    this.jobRecords.set(id, {...job, options: {...job.options, ...patch}});
  }

  async claimRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<JobRecord | undefined> {
    const now = this.now().toISOString();
    const expiresAt = new Date(this.now().getTime() + leaseDurationMs).toISOString();
    const job = this.jobRecords.get(id);
    if (!job || job.taskType !== 'project.render' || ['COMPLETED', 'FAILED', 'CANCELLED'].includes(job.status)) return undefined;
    if (job.status === 'RUNNING' && job.renderLeaseExpiresAt !== undefined && job.renderLeaseExpiresAt > now) return undefined;
    const claimed = {...job, status: 'RUNNING' as const, renderLeaseOwner: owner, renderLeaseExpiresAt: expiresAt};
    this.jobRecords.set(id, claimed);
    return clone(claimed);
  }

  async renewRenderJobLease(id: string, owner: string, leaseDurationMs: number): Promise<JobRecord | undefined> {
    const now = this.now().toISOString();
    const expiresAt = new Date(this.now().getTime() + leaseDurationMs).toISOString();
    const job = this.jobRecords.get(id);
    if (!job || job.status !== 'RUNNING' || job.renderLeaseOwner !== owner || !job.renderLeaseExpiresAt || job.renderLeaseExpiresAt <= now || expiresAt <= now || expiresAt <= job.renderLeaseExpiresAt) return undefined;
    const renewed = {...job, renderLeaseExpiresAt: expiresAt};
    this.jobRecords.set(id, renewed);
    return clone(renewed);
  }

  async commitRenderJobTerminal(commit: RenderJobTerminalCommit): Promise<RenderJobTerminalCommitResult | undefined> {
    const job = this.jobRecords.get(commit.jobId);
    const project = this.projectRecords.get(commit.projectId);
    const now = this.now().toISOString();
    if (!job || !project || job.projectId !== commit.projectId || job.status !== 'RUNNING' ||
        job.renderLeaseOwner !== commit.owner || !job.renderLeaseExpiresAt || job.renderLeaseExpiresAt <= now) return undefined;
    const terminalProject = project.status === 'COMPLETED' || project.status === 'FAILED' || project.status === 'CANCELLED';
    const canCommit = terminalProject
      ? project.status === commit.status
      : commit.status === 'COMPLETED' ? project.status === 'QUALITY_CHECK' : true;
    if (!canCommit) return undefined;
    const committedProject = terminalProject
      ? {...project, updatedAt: now}
      : {...project, status: commit.status, ...(commit.reportPath ? {qualityReportPath: commit.reportPath} : {}), ...(commit.output ?? {}), updatedAt: now};
    const committedJob = {...job, status: commit.status, renderLeaseOwner: undefined, renderLeaseExpiresAt: undefined};
    this.projectRecords.set(project.id, committedProject);
    this.jobRecords.set(job.id, committedJob);
    return {project: clone(committedProject), job: clone(committedJob)};
  }

  async listAssetVersions(projectId: string, sceneId: string): Promise<AssetVersionRecord[]> {
    return [...this.assetVersionRecords.values()]
      .filter((asset) => asset.projectId === projectId && asset.sceneId === sceneId)
      .sort((left, right) => left.version - right.version)
      .map(clone);
  }

  async createAssetVersion(asset: AssetVersionRecord): Promise<void> {
    this.assetVersionRecords.set(asset.id, clone(asset));
  }
}

export class ProjectService {
  constructor(
    private readonly repository: ProjectRepository,
    private readonly queue: QueueAdapter,
    private readonly idFactory: () => string,
    private readonly now: () => Date = () => new Date()
  ) {}

  async create(input: ProjectInput, ownerUsername: string): Promise<ProjectRecord> {
    const now = this.now().toISOString();
    const project: ProjectRecord = {
      ...clone(input),
      id: this.idFactory(),
      ownerUsername,
      title: input.input.content.slice(0, 80),
      status: 'CREATED',
      createdAt: now,
      updatedAt: now
    };

    const initialJob = this.jobFor(project, 'project', 'storyboard.generate', input);
    if (this.repository.createProjectWithInitialJobQuota) {
      const created = await this.repository.createProjectWithInitialJobQuota(project, initialJob, 10);
      if (!created) throw new ProjectQuotaExceededError();
    } else {
      await this.repository.transaction(async () => {
        await this.repository.createProject(project);
        await this.repository.reserveJob(initialJob);
      });
    }
    try {
      await this.enqueue(initialJob, input);
    } catch {
      // The committed PENDING job is retried by the outbox/worker boundary.
    }
    return project;
  }

  get(id: string, ownerUsername: string): Promise<ProjectDetail | undefined> {
    return this.repository.findProject(id, ownerUsername);
  }

  async patchScene(projectId: string, sceneId: string, patch: EditableScenePatch, ownerUsername: string): Promise<SceneRecord | undefined> {
    return this.repository.updateScene(projectId, sceneId, patch, ownerUsername);
  }

  updateOutput(projectId: string, output: {previewUrl?: string; downloadUrl?: string}): Promise<ProjectRecord | undefined> {
    return this.repository.updateProjectOutput(projectId, output);
  }

  markSceneFailure(projectId: string, sceneId: string, generationJobId: string, failureReason: string): Promise<TerminalProjectFailureResult | undefined> {
    return this.repository.markSceneFailedAndProjectPartiallyFailed(projectId, sceneId, generationJobId, failureReason);
  }

  async regenerateScene(project: ProjectDetail, scene: SceneRecord): Promise<QueueSubmission> {
    return this.submit(project, scene.id, 'scene.regenerate', scene);
  }

  async render(project: ProjectDetail): Promise<QueueSubmission> {
    return this.submit(project, 'project', 'project.render', {output: project.output, scenes: project.scenes});
  }

  async retryStoryboard(projectId: string): Promise<QueueSubmission | undefined> {
    const project = await this.repository.findProjectForWorker(projectId);
    if (!project) return undefined;
    return this.submit(project, 'project', 'storyboard.generate', {
      input: project.input,
      avatar: project.avatar,
      output: project.output
    });
  }

  private jobFor(project: ProjectRecord, sceneId: string, taskType: string, input: unknown): JobRecord {
    const inputHash = canonicalInputHash(input);
    return {id: jobKey(project.id, sceneId, taskType, inputHash), projectId: project.id, sceneId, taskType, inputHash, dispatchPayload: clone(input), status: 'PENDING', createdAt: this.now().toISOString()};
  }

  private async submit(project: ProjectRecord, sceneId: string, taskType: string, input: unknown): Promise<QueueSubmission> {
    const reserved = await this.repository.reserveJob(this.jobFor(project, sceneId, taskType, input));
    return this.enqueue(reserved.job, input);
  }

  private async enqueue(job: JobRecord, input: unknown): Promise<QueueSubmission> {
    if (job.status !== 'PENDING') return {id: job.id, existing: true};
    const submitted = await this.queue.submit({id: job.id, name: job.taskType, data: {projectId: job.projectId, sceneId: job.sceneId, input}});
    const queued = await this.repository.markJobQueued(job.id);
    if (!queued) {
      const current = await this.repository.findJob(job.id);
      if (current && current.status !== 'PENDING') return {id: job.id, existing: true};
      throw new Error(`job ${job.id} could not be queued from PENDING`);
    }
    return {...submitted, id: job.id, existing: false};
  }
}

export class ProjectQuotaExceededError extends Error {
  readonly name = 'ProjectQuotaExceededError';
  constructor() {
    super('active project quota exceeded');
  }
}
