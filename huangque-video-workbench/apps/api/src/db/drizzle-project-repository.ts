import {and, eq, inArray, sql} from 'drizzle-orm';
import {drizzle, type NodePgDatabase} from 'drizzle-orm/node-postgres';
import {AsyncLocalStorage} from 'node:async_hooks';
import {Pool} from 'pg';
import {assetVersions, jobs, projects, scenes} from './schema.js';
import {REGENERATION_ACTIVATION_PROJECT_SOURCES, REGENERATION_ACTIVATION_SCENE_SOURCES, TERMINAL_FAILURE_TRANSITION_SOURCES} from '../services/project-service.js';
import type {AssetVersionRecord, AssetVersionWrite, JobGenerationStatePatch, JobQualityClaimPatch, JobRecord, ProjectDetail, ProjectRecord, ProjectRepository, RegenerationProjectStatus, ScenePatch, SceneRecord, SceneRegenerationActivationResult, TerminalProjectFailureResult} from '../services/project-service.js';

type Database = NodePgDatabase;
type ProjectRow = typeof projects.$inferSelect;
type SceneRow = typeof scenes.$inferSelect;
type JobRow = typeof jobs.$inferSelect;
type AssetVersionRow = typeof assetVersions.$inferSelect;

const asProject = (row: ProjectRow): ProjectRecord => ({
  id: row.id,
  status: row.status as ProjectRecord['status'],
  title: row.title,
  input: row.input,
  avatar: row.avatar,
  output: row.output,
  qualityReportPath: row.qualityReportPath ?? undefined,
  previewUrl: row.previewUrl ?? undefined,
  downloadUrl: row.downloadUrl ?? undefined,
  createdAt: row.createdAt.toISOString(),
  updatedAt: row.updatedAt.toISOString()
});

const asScene = (row: SceneRow): SceneRecord => ({
  id: row.id,
  projectId: row.projectId,
  order: row.order,
  status: row.status,
  script: row.script,
  visual: row.visual,
  asset: row.asset,
  failureReason: row.failureReason ?? undefined,
  createdAt: row.createdAt.toISOString(),
  updatedAt: row.updatedAt.toISOString()
});

const asJob = (row: JobRow): JobRecord => ({
  id: row.id,
  projectId: row.projectId,
  sceneId: row.sceneId,
  taskType: row.taskType,
  inputHash: row.inputHash,
  options: row.options ?? undefined,
  status: row.status as JobRecord['status'],
  createdAt: row.createdAt.toISOString()
});

const asAssetVersion = (row: AssetVersionRow): AssetVersionRecord => ({
  id: row.id, projectId: row.projectId, sceneId: row.sceneId, version: row.version,
  uri: row.uri, provenance: row.provenance as AssetVersionRecord['provenance'], inputHash: row.inputHash, createdAt: row.createdAt.toISOString()
});

class TerminalFailureStatusConflict extends Error {}
class TerminalFailureGenerationMismatch extends Error {}
class SceneRegenerationProjectConflict extends Error {}
class SceneRegenerationActivationConflict extends Error {}

/** PostgreSQL/Drizzle implementation; route tests use InMemoryProjectRepository. */
export class DrizzleProjectRepository implements ProjectRepository {
  private readonly transactions = new AsyncLocalStorage<Database>();

  constructor(private readonly db: Database) {}

  private get current(): Database {
    return this.transactions.getStore() ?? this.db;
  }

  async transaction<T>(work: () => Promise<T>): Promise<T> {
    return this.db.transaction(async (transaction) => this.transactions.run(transaction as unknown as Database, work));
  }

  async createProject(project: ProjectRecord): Promise<void> {
    await this.current.insert(projects).values({...project, createdAt: new Date(project.createdAt), updatedAt: new Date(project.updatedAt)});
  }

  async findProject(id: string): Promise<ProjectDetail | undefined> {
    const [project] = await this.current.select().from(projects).where(eq(projects.id, id));
    if (!project) return undefined;
    const [sceneRows, jobRows, assetRows] = await Promise.all([
      this.current.select().from(scenes).where(eq(scenes.projectId, id)).orderBy(scenes.order),
      this.current.select().from(jobs).where(eq(jobs.projectId, id)),
      this.current.select().from(assetVersions).where(eq(assetVersions.projectId, id))
    ]);
    return {...asProject(project), scenes: sceneRows.map(asScene), jobs: jobRows.map(asJob), assetVersions: assetRows.map(asAssetVersion)};
  }

  async updateProjectStatus(projectId: string, status: ProjectRecord['status']): Promise<void> {
    await this.current.update(projects).set({status, updatedAt: new Date()}).where(eq(projects.id, projectId));
  }

  async claimProjectStatus(
    projectId: string,
    expectedStatus: ProjectRecord['status'],
    nextStatus: ProjectRecord['status']
  ): Promise<ProjectRecord | undefined> {
    const [claimed] = await this.current.update(projects)
      .set({status: nextStatus, updatedAt: new Date()})
      .where(and(eq(projects.id, projectId), eq(projects.status, expectedStatus)))
      .returning();
    return claimed && asProject(claimed);
  }

  async claimProjectQualityResult(
    projectId: string,
    nextStatus: Extract<ProjectRecord['status'], 'COMPLETED' | 'FAILED'>,
    qualityReportPath: string, output?: {previewUrl?: string; downloadUrl?: string}
  ): Promise<ProjectRecord | undefined> {
    const [claimed] = await this.current.update(projects)
      .set({status: nextStatus, qualityReportPath, ...output, updatedAt: new Date()})
      .where(and(eq(projects.id, projectId), eq(projects.status, 'QUALITY_CHECK')))
      .returning();
    return claimed && asProject(claimed);
  }
  async updateProjectOutput(projectId: string, output: {previewUrl?: string; downloadUrl?: string}): Promise<ProjectRecord | undefined> {
    const [updated] = await this.current.update(projects).set({...output, updatedAt: new Date()}).where(eq(projects.id, projectId)).returning();
    return updated && asProject(updated);
  }

  async markSceneFailedAndProjectPartiallyFailed(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string, failureReason?: string
  ): Promise<TerminalProjectFailureResult | undefined> {
    for (;;) {
      try {
        return await this.transaction(async () => {
          const [project] = await this.current.select().from(projects).where(eq(projects.id, projectId));
          if (!project) return undefined;
          const transitionsProject = (TERMINAL_FAILURE_TRANSITION_SOURCES as readonly string[]).includes(project.status);
          const preservesProject = project.status === 'NEEDS_USER_INPUT' || project.status === 'PARTIALLY_FAILED';
          if (!transitionsProject && !preservesProject) {
            return {project: asProject(project), transitioned: false, sceneUpdated: false};
          }

          const [failedProject] = await this.current.update(projects)
            .set({status: transitionsProject ? 'PARTIALLY_FAILED' : project.status, updatedAt: new Date()})
            .where(and(
              eq(projects.id, projectId),
              eq(projects.status, project.status),
              transitionsProject
                ? inArray(projects.status, [...TERMINAL_FAILURE_TRANSITION_SOURCES])
                : inArray(projects.status, ['NEEDS_USER_INPUT', 'PARTIALLY_FAILED'])
            ))
            .returning();
          if (!failedProject) throw new TerminalFailureStatusConflict();

          const [failedScene] = await this.current.update(scenes)
            .set({status: 'FAILED', ...(failureReason ? {failureReason} : {}), updatedAt: new Date()})
            .where(and(
              eq(scenes.projectId, projectId),
              eq(scenes.id, sceneId),
              eq(scenes.status, 'GENERATING'),
              sql`${scenes.visual}->>'activeGenerationJobId' = ${expectedGenerationJobId}`
            ))
            .returning();
          if (!failedScene) throw new TerminalFailureGenerationMismatch();
          return {project: asProject(failedProject), transitioned: transitionsProject, sceneUpdated: true};
        });
      } catch (error) {
        if (error instanceof TerminalFailureStatusConflict) continue;
        if (error instanceof TerminalFailureGenerationMismatch) {
          const project = await this.findProject(projectId);
          if (!project) return undefined;
          if (!project.scenes.some((scene) => scene.id === sceneId)) {
            throw new Error(`scene ${sceneId} was not found in project ${projectId}`);
          }
          return {project, transitioned: false, sceneUpdated: false};
        }
        throw error;
      }
    }
  }

  async createScene(scene: SceneRecord): Promise<void> {
    await this.current.insert(scenes).values({...scene, createdAt: new Date(scene.createdAt), updatedAt: new Date(scene.updatedAt)});
  }

  async activateSceneRegeneration(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string | undefined,
    nextGenerationJobId: string,
    nextProjectStatus: RegenerationProjectStatus
  ): Promise<SceneRegenerationActivationResult | undefined> {
    const activeGenerationPredicate = expectedGenerationJobId === undefined
      ? sql`${scenes.visual}->>'activeGenerationJobId' is null`
      : sql`${scenes.visual}->>'activeGenerationJobId' = ${expectedGenerationJobId}`;
    for (;;) {
      try {
        return await this.transaction(async () => {
          const [project] = await this.current.select().from(projects).where(eq(projects.id, projectId));
          if (!project || !(REGENERATION_ACTIVATION_PROJECT_SOURCES as readonly string[]).includes(project.status)) {
            return undefined;
          }
          const [activatedProject] = await this.current.update(projects)
            .set({status: nextProjectStatus, updatedAt: new Date()})
            .where(and(
              eq(projects.id, projectId),
              eq(projects.status, project.status),
              inArray(projects.status, [...REGENERATION_ACTIVATION_PROJECT_SOURCES])
            ))
            .returning();
          if (!activatedProject) throw new SceneRegenerationProjectConflict();
          const [activatedScene] = await this.current.update(scenes)
            .set({
              status: 'PENDING',
              failureReason: null,
              visual: sql`${scenes.visual} || ${JSON.stringify({activeGenerationJobId: nextGenerationJobId})}::jsonb`,
              updatedAt: new Date()
            })
            .where(and(
              eq(scenes.projectId, projectId),
              eq(scenes.id, sceneId),
              inArray(scenes.status, [...REGENERATION_ACTIVATION_SCENE_SOURCES]),
              activeGenerationPredicate
            ))
            .returning();
          if (!activatedScene) throw new SceneRegenerationActivationConflict();
          return {
            project: asProject(activatedProject),
            scene: asScene(activatedScene),
            transitioned: project.status !== nextProjectStatus
          };
        });
      } catch (error) {
        if (error instanceof SceneRegenerationProjectConflict) continue;
        if (error instanceof SceneRegenerationActivationConflict) return undefined;
        throw error;
      }
    }
  }

  async claimSceneActiveGenerationJob(
    projectId: string,
    sceneId: string,
    expectedGenerationJobId: string | undefined,
    nextGenerationJobId: string
  ): Promise<SceneRecord | undefined> {
    const activeGenerationPredicate = expectedGenerationJobId === undefined
      ? sql`${scenes.visual}->>'activeGenerationJobId' is null`
      : sql`${scenes.visual}->>'activeGenerationJobId' = ${expectedGenerationJobId}`;
    const [updated] = await this.current.update(scenes)
      .set({
        visual: sql`${scenes.visual} || ${JSON.stringify({activeGenerationJobId: nextGenerationJobId})}::jsonb`,
        updatedAt: new Date()
      })
      .where(and(eq(scenes.projectId, projectId), eq(scenes.id, sceneId), activeGenerationPredicate))
      .returning();
    return updated ? asScene(updated) : undefined;
  }

  async beginSceneGeneration(
    projectId: string,
    sceneId: string,
    generationJobId: string
  ): Promise<SceneRecord | undefined> {
    const [updated] = await this.current.update(scenes)
      .set({status: 'GENERATING', updatedAt: new Date()})
      .where(and(
        eq(scenes.projectId, projectId),
        eq(scenes.id, sceneId),
        inArray(scenes.status, ['PENDING', 'GENERATING', 'NEEDS_USER_INPUT', 'FAILED']),
        sql`${scenes.visual}->>'activeGenerationJobId' = ${generationJobId}`
      ))
      .returning();
    return updated ? asScene(updated) : undefined;
  }

  async completeSceneGeneration(
    projectId: string,
    sceneId: string,
    generationJobId: string,
    asset: AssetVersionWrite
  ): Promise<AssetVersionRecord | undefined> {
    return this.transaction(async () => {
      const [completedScene] = await this.current.update(scenes)
        .set({status: 'READY', updatedAt: new Date()})
        .where(and(
          eq(scenes.projectId, projectId),
          eq(scenes.id, sceneId),
          eq(scenes.status, 'GENERATING'),
          sql`${scenes.visual}->>'activeGenerationJobId' = ${generationJobId}`
        ))
        .returning();
      if (!completedScene) return undefined;
      const existing = await this.current.select().from(assetVersions)
        .where(and(eq(assetVersions.projectId, projectId), eq(assetVersions.sceneId, sceneId)));
      const version = Math.max(0, ...existing.map((candidate) => candidate.version)) + 1;
      const record: AssetVersionRecord = {
        ...asset, id: `${projectId}:${sceneId}:v${version}`, projectId, sceneId, version
      };
      const [created] = await this.current.insert(assetVersions)
        .values({...record, createdAt: new Date(record.createdAt)})
        .returning();
      return created ? asAssetVersion(created) : undefined;
    });
  }

  async updateScene(projectId: string, sceneId: string, patch: ScenePatch): Promise<SceneRecord | undefined> {
    const [updated] = await this.current.update(scenes)
      .set({...patch, updatedAt: new Date()})
      .where(and(eq(scenes.projectId, projectId), eq(scenes.id, sceneId)))
      .returning();
    return updated ? asScene(updated) : undefined;
  }

  async reserveJob(job: JobRecord): Promise<{job: JobRecord; existing: boolean}> {
    const [created] = await this.current.insert(jobs)
      .values({...job, createdAt: new Date(job.createdAt)})
      .onConflictDoNothing()
      .returning();
    if (created) return {job: asJob(created), existing: false};
    const [existing] = await this.current.select().from(jobs).where(eq(jobs.id, job.id));
    if (!existing) throw new Error(`job reservation failed for ${job.id}`);
    return {job: asJob(existing), existing: true};
  }

  async findJob(id: string): Promise<JobRecord | undefined> {
    const [job] = await this.current.select().from(jobs).where(eq(jobs.id, id));
    return job && asJob(job);
  }

  async claimJobDelivery(id: string, expectedDeliveryAttemptsMade: number, nextDeliveryAttemptsMade: number): Promise<JobRecord | undefined> {
    const [claimed] = await this.current.update(jobs)
      .set({options: sql`${jobs.options} || ${JSON.stringify({deliveryAttemptsMade: nextDeliveryAttemptsMade})}::jsonb`})
      .where(and(
        eq(jobs.id, id),
        sql`coalesce((${jobs.options}->>'deliveryAttemptsMade')::integer, 0) = ${expectedDeliveryAttemptsMade}`,
        sql`coalesce((${jobs.options}->>'qualityTerminal')::boolean, false) = false`
      ))
      .returning();
    return claimed && asJob(claimed);
  }

  async claimJobQuality(id: string, expectedQualityAttempt: number, patch: JobQualityClaimPatch): Promise<JobRecord | undefined> {
    const [claimed] = await this.current.update(jobs)
      .set({options: sql`${jobs.options} || ${JSON.stringify(patch)}::jsonb`})
      .where(and(
        eq(jobs.id, id),
        sql`coalesce((${jobs.options}->>'qualityAttempt')::integer, 1) = ${expectedQualityAttempt}`,
        sql`coalesce((${jobs.options}->>'qualityTerminal')::boolean, false) = false`
      ))
      .returning();
    return claimed && asJob(claimed);
  }

  async markJobQueued(id: string): Promise<void> {
    await this.current.update(jobs).set({status: 'QUEUED'}).where(eq(jobs.id, id));
  }

  async updateJobQualityAttempt(id: string, qualityAttempt: number): Promise<void> {
    await this.updateJobGenerationState(id, {qualityAttempt});
  }

  async updateJobGenerationState(id: string, patch: JobGenerationStatePatch): Promise<void> {
    const [job] = await this.current.select().from(jobs).where(eq(jobs.id, id));
    if (!job?.options) throw new Error(`job ${id} has no generation options`);
    await this.current.update(jobs).set({options: {...job.options, ...patch}}).where(eq(jobs.id, id));
  }

  async listAssetVersions(projectId: string, sceneId: string): Promise<AssetVersionRecord[]> {
    return (await this.current.select().from(assetVersions).where(and(eq(assetVersions.projectId, projectId), eq(assetVersions.sceneId, sceneId))))
      .sort((left, right) => left.version - right.version).map(asAssetVersion);
  }

  async createAssetVersion(asset: AssetVersionRecord): Promise<void> {
    await this.current.insert(assetVersions).values({...asset, createdAt: new Date(asset.createdAt)});
  }
}

export const createPostgresProjectRepository = (connectionString: string): {repository: DrizzleProjectRepository; pool: Pool} => {
  const pool = new Pool({connectionString});
  return {repository: new DrizzleProjectRepository(drizzle({client: pool})), pool};
};
