import {z} from 'zod';

const StatusSchema = z.enum([
  'CREATED', 'STORYBOARDING', 'GENERATING_ASSETS', 'GENERATING_AVATAR', 'ALIGNING_TIMELINE', 'RENDERING',
  'QUALITY_CHECK', 'COMPLETED', 'RETRYING', 'PARTIALLY_FAILED', 'NEEDS_USER_INPUT', 'FAILED', 'CANCELLED'
]);

export const WorkbenchVisualSchema = z.record(z.string(), z.unknown());
export const WorkbenchSceneSchema = z.object({
  id: z.string().min(1),
  order: z.number().int().positive(),
  status: z.string().min(1),
  script: z.string(),
  visual: WorkbenchVisualSchema,
  asset: z.record(z.string(), z.unknown()).nullable().optional(),
  locked: z.boolean().optional(),
  failureReason: z.string().min(1).optional()
});
export const AssetVersionSchema = z.object({
  id: z.string().min(1),
  projectId: z.string().min(1).optional(),
  sceneId: z.string().min(1),
  version: z.number().int().positive(),
  uri: z.string().min(1),
  provenance: z.string().min(1).optional(),
  inputHash: z.string().min(1).optional(),
  createdAt: z.string().min(1).optional()
});
export const WorkbenchProjectSchema = z.object({
  id: z.string().min(1),
  title: z.string(),
  status: StatusSchema,
  scenes: z.array(WorkbenchSceneSchema),
  assetVersions: z.array(AssetVersionSchema),
  previewUrl: z.string().min(1).optional(),
  downloadUrl: z.string().min(1).optional()
});

export type WorkbenchScene = z.infer<typeof WorkbenchSceneSchema>;
export type AssetVersion = z.infer<typeof AssetVersionSchema>;
export type WorkbenchProject = z.infer<typeof WorkbenchProjectSchema>;
export const parseWorkbenchProject = (value: unknown): WorkbenchProject | undefined => {
  const parsed = WorkbenchProjectSchema.safeParse(value);
  return parsed.success ? parsed.data : undefined;
};
