import {z} from 'zod';

export const SceneTypeSchema = z.enum(['avatar', 'image', 'image_video', 'stock_video', 'upload', 'chart', 'screenshot', 'text_fallback']);

export const SceneSchema = z.object({
  id: z.string().min(1).max(128),
  order: z.number().int().positive(),
  type: SceneTypeSchema,
  purpose: z.string().trim().min(1).max(500),
  script: z.string().min(1).max(10_000),
  visualPrompt: z.string().min(1).max(4_000).optional(),
  durationEstimate: z.number().positive(),
  visual: z.object({
    layout: z.string().min(1).max(64),
    headline: z.string().max(200).nullable().optional(),
    highlightWords: z.array(z.string().min(1).max(64)).max(20)
  }),
  asset: z.object({
    source: z.string(),
    query: z.string().optional(),
    prompt: z.string().optional(),
    factual: z.boolean().default(false)
  }).nullable().optional()
}).superRefine((scene, ctx) => {
  if (scene.type === 'avatar' && scene.durationEstimate > 12) {
    ctx.addIssue({code: 'custom', message: 'avatar scene exceeds 12 seconds'});
  }
});

const EditablePresentationSchema = z.object({
  layout: z.string().min(1).max(64).optional(),
  headline: z.string().min(1).max(200).nullable().optional(),
  highlightWords: z.array(z.string().min(1).max(64)).max(20).optional()
}).strict().refine((value) => Object.keys(value).length > 0, 'at least one presentation field is required');

/** The only scene fields an editor may change. Orchestration state is never client input. */
export const EditableScenePatchSchema = z.object({
  script: z.string().trim().min(1).max(10_000).optional(),
  visualPrompt: z.string().trim().min(1).max(4_000).optional(),
  visual: EditablePresentationSchema.optional()
}).strict().refine((value) => Object.keys(value).length > 0, 'at least one scene field is required');

export const StoryboardSchema = z.object({
  project: z.object({
    title: z.string().max(120),
    width: z.literal(1080),
    height: z.literal(1920),
    fps: z.literal(30)
  }),
  scenes: z.array(SceneSchema).min(1).max(100)
});

export type Scene = z.infer<typeof SceneSchema>;
export type Storyboard = z.infer<typeof StoryboardSchema>;
export type EditableScenePatch = z.infer<typeof EditableScenePatchSchema>;
