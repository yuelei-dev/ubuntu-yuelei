import {z} from 'zod';

export const SceneTypeSchema = z.enum(['avatar', 'image', 'image_video', 'stock_video', 'upload', 'chart', 'screenshot', 'text_fallback']);

export const SceneSchema = z.object({
  id: z.string().min(1),
  order: z.number().int().positive(),
  type: SceneTypeSchema,
  purpose: z.string().min(1),
  script: z.string().min(1),
  durationEstimate: z.number().positive(),
  visual: z.object({
    layout: z.string().min(1),
    headline: z.string().nullable().optional(),
    highlightWords: z.array(z.string())
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

export const StoryboardSchema = z.object({
  project: z.object({
    title: z.string(),
    width: z.literal(1080),
    height: z.literal(1920),
    fps: z.literal(30)
  }),
  scenes: z.array(SceneSchema).min(1)
});

export type Scene = z.infer<typeof SceneSchema>;
export type Storyboard = z.infer<typeof StoryboardSchema>;
