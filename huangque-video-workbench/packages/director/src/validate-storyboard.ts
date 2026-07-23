import type {Storyboard} from '@huangque/contracts';

const generatedSources = new Set(['generate', 'generated']);

export const validateStoryboard = (board: Storyboard): string[] => {
  const errors: string[] = [];
  let secondsSinceVisual = 0;

  for (const scene of board.scenes) {
    if (scene.type === 'avatar' && scene.durationEstimate > 12) {
      errors.push(`${scene.id}: avatar scene exceeds 12 seconds`);
    }

    if (
      (scene.type === 'screenshot' || scene.type === 'upload') &&
      scene.asset?.factual &&
      generatedSources.has(scene.asset.source)
    ) {
      errors.push(`${scene.id}: factual screenshot/upload scene cannot request generated media`);
    }

    if (scene.type === 'avatar') {
      secondsSinceVisual += scene.durationEstimate;
      if (secondsSinceVisual > 15) {
        errors.push(`${scene.id}: more than 15 seconds without a visual-type scene`);
      }
    } else {
      secondsSinceVisual = 0;
    }
  }

  return errors;
};
