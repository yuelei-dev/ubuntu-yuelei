import type {SceneJob, SceneJobProcessor} from './registry.js';

export const isAvatarScene = (scene: {type: string}): boolean => scene.type === 'avatar';

export const createAvatarGenerateProcessor = (
  generate: (job: SceneJob, kind: 'avatar') => Promise<void>
): SceneJobProcessor => ({
  process: (job) => generate(job, 'avatar')
});
