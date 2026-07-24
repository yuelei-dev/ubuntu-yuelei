import {z} from 'zod';

export const ProjectStatusSchema = z.enum([
  'CREATED',
  'STORYBOARDING',
  'GENERATING_ASSETS',
  'GENERATING_AVATAR',
  'ALIGNING_TIMELINE',
  'RENDERING',
  'QUALITY_CHECK',
  'COMPLETED',
  'RETRYING',
  'PARTIALLY_FAILED',
  'NEEDS_USER_INPUT',
  'FAILED',
  'CANCELLED'
]);

export type ProjectStatus = z.infer<typeof ProjectStatusSchema>;
