import {z} from 'zod';
import {EditableScenePatchSchema} from '@huangque/contracts';
import {ProjectQuotaExceededError, type ProjectService, type SceneRecord} from '../services/project-service.js';

const IdSchema = z.string().regex(/^[A-Za-z0-9_-]+$/, 'must contain only letters, numbers, underscores, or hyphens');
const CreateProjectSchema = z.object({
  input: z.object({type: z.literal('script'), content: z.string().trim().min(1).max(100_000)}),
  avatar: z.object({avatarId: z.string().trim().min(1).max(128), voiceId: z.string().trim().min(1).max(128)}),
  output: z.object({templateId: z.string().trim().min(1).max(128)})
});
export type ApiResponse = {statusCode: number; payload: unknown};

const validationError = (issues: z.core.$ZodIssue[]): ApiResponse => ({
  statusCode: 400,
  payload: {error: 'validation_error', issues: issues.map((issue) => ({path: issue.path.join('.'), message: issue.message}))}
});

const missing = (resource: string): ApiResponse => ({statusCode: 404, payload: {error: 'not_found', resource}});

const parseIds = (projectId: string, sceneId?: string): ApiResponse | {projectId: string; sceneId?: string} => {
  const project = IdSchema.safeParse(projectId);
  const scene = sceneId === undefined ? undefined : IdSchema.safeParse(sceneId);
  if (!project.success) return validationError(project.error.issues);
  if (scene && !scene.success) return validationError(scene.error.issues);
  return {projectId: project.data, sceneId: scene?.data};
};

export const projectRoutes = (service: ProjectService) => ({
  async create(payload: unknown, ownerUsername: string): Promise<ApiResponse> {
    const input = CreateProjectSchema.safeParse(payload);
    if (!input.success) return validationError(input.error.issues);
    try {
      const project = await service.create(input.data, ownerUsername);
      return {statusCode: 202, payload: project};
    } catch (error) {
      if (error instanceof ProjectQuotaExceededError) {
        return {statusCode: 429, payload: {error: 'active_project_quota_exceeded', limit: 10}};
      }
      throw error;
    }
  },

  async get(projectId: string, ownerUsername: string): Promise<ApiResponse> {
    const ids = parseIds(projectId);
    if ('statusCode' in ids) return ids;
    const project = await service.get(ids.projectId, ownerUsername);
    return project ? {statusCode: 200, payload: project} : missing('project');
  },

  async patchScene(projectId: string, sceneId: string, payload: unknown, ownerUsername: string): Promise<ApiResponse> {
    const ids = parseIds(projectId, sceneId);
    if ('statusCode' in ids) return ids;
    const patch = EditableScenePatchSchema.safeParse(payload);
    if (!patch.success) return validationError(patch.error.issues);
    const project = await service.get(ids.projectId, ownerUsername);
    if (!project) return missing('project');
    const scene = await service.patchScene(ids.projectId, ids.sceneId!, patch.data, ownerUsername);
    return scene ? {statusCode: 200, payload: scene} : missing('scene');
  },

  async regenerate(projectId: string, sceneId: string, ownerUsername: string): Promise<ApiResponse> {
    const ids = parseIds(projectId, sceneId);
    if ('statusCode' in ids) return ids;
    const project = await service.get(ids.projectId, ownerUsername);
    if (!project) return missing('project');
    const scene = project.scenes.find((candidate: SceneRecord) => candidate.id === ids.sceneId);
    if (!scene) return missing('scene');
    const job = await service.regenerateScene(project, scene);
    return {statusCode: 202, payload: {jobId: job.id, existing: job.existing}};
  },

  async render(projectId: string, ownerUsername: string): Promise<ApiResponse> {
    const ids = parseIds(projectId);
    if ('statusCode' in ids) return ids;
    const project = await service.get(ids.projectId, ownerUsername);
    if (!project) return missing('project');
    const job = await service.render(project);
    return {statusCode: 202, payload: {jobId: job.id, existing: job.existing}};
  }
});
