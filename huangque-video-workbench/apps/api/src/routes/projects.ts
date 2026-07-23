import {z} from 'zod';
import type {ProjectService, SceneRecord} from '../services/project-service.js';

const IdSchema = z.string().regex(/^[A-Za-z0-9_-]+$/, 'must contain only letters, numbers, underscores, or hyphens');
const CreateProjectSchema = z.object({
  input: z.object({type: z.literal('script'), content: z.string().trim().min(1)}),
  avatar: z.object({avatarId: z.string().trim().min(1), voiceId: z.string().trim().min(1)}),
  output: z.object({templateId: z.string().trim().min(1)})
});
const ScenePatchSchema = z.object({
  script: z.string().trim().min(1).optional(),
  visual: z.record(z.string(), z.unknown()).optional(),
  asset: z.record(z.string(), z.unknown()).nullable().optional()
}).refine((value) => Object.keys(value).length > 0, 'at least one scene field is required');

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
  async create(payload: unknown): Promise<ApiResponse> {
    const input = CreateProjectSchema.safeParse(payload);
    if (!input.success) return validationError(input.error.issues);
    const project = await service.create(input.data);
    return {statusCode: 202, payload: project};
  },

  async get(projectId: string): Promise<ApiResponse> {
    const ids = parseIds(projectId);
    if ('statusCode' in ids) return ids;
    const project = await service.get(ids.projectId);
    return project ? {statusCode: 200, payload: project} : missing('project');
  },

  async patchScene(projectId: string, sceneId: string, payload: unknown): Promise<ApiResponse> {
    const ids = parseIds(projectId, sceneId);
    if ('statusCode' in ids) return ids;
    const patch = ScenePatchSchema.safeParse(payload);
    if (!patch.success) return validationError(patch.error.issues);
    const project = await service.get(ids.projectId);
    if (!project) return missing('project');
    const scene = await service.patchScene(ids.projectId, ids.sceneId!, patch.data);
    return scene ? {statusCode: 200, payload: scene} : missing('scene');
  },

  async regenerate(projectId: string, sceneId: string): Promise<ApiResponse> {
    const ids = parseIds(projectId, sceneId);
    if ('statusCode' in ids) return ids;
    const project = await service.get(ids.projectId);
    if (!project) return missing('project');
    const scene = project.scenes.find((candidate: SceneRecord) => candidate.id === ids.sceneId);
    if (!scene) return missing('scene');
    const job = await service.regenerateScene(project, scene);
    return {statusCode: 202, payload: {jobId: job.id, existing: job.existing}};
  },

  async render(projectId: string): Promise<ApiResponse> {
    const ids = parseIds(projectId);
    if ('statusCode' in ids) return ids;
    const project = await service.get(ids.projectId);
    if (!project) return missing('project');
    const job = await service.render(project);
    return {statusCode: 202, payload: {jobId: job.id, existing: job.existing}};
  }
});
