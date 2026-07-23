import type {CaptionWord, Timeline} from './types.js';

const captionLabel = (word: CaptionWord): string => word.text ?? word.word ?? '<unnamed>';

export const validateTimeline = (timeline: Timeline): string[] => {
  const errors: string[] = [];

  if (timeline.fps !== 30) {
    errors.push(`unsupported fps: ${timeline.fps}`);
  }

  let expectedStartFrame = 0;
  for (const scene of timeline.scenes) {
    if (scene.startFrame > expectedStartFrame) {
      errors.push(`scene ${scene.id} has a gap before frame ${scene.startFrame}`);
    } else if (scene.startFrame < expectedStartFrame) {
      errors.push(`scene ${scene.id} overlaps the previous scene at frame ${scene.startFrame}`);
    }

    if (!Number.isFinite(scene.durationInFrames) || scene.durationInFrames <= 0) {
      errors.push(`scene ${scene.id} has a non-positive duration`);
    }
    if (scene.endFrame !== scene.startFrame + scene.durationInFrames) {
      errors.push(`scene ${scene.id} has inconsistent end frame`);
    }

    const sceneDurationMs = scene.durationInFrames / timeline.fps * 1000;
    for (const word of scene.words ?? []) {
      const label = captionLabel(word);
      if (!Number.isFinite(word.startMs) || !Number.isFinite(word.endMs) || word.endMs <= word.startMs) {
        errors.push(`caption ${label} in scene ${scene.id} has an invalid interval`);
      }
      if (word.startMs < 0 || word.endMs > sceneDurationMs) {
        errors.push(`caption ${label} in scene ${scene.id} is outside the scene`);
      }
    }

    expectedStartFrame = scene.endFrame;
  }

  return errors;
};
