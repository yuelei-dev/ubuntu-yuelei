import type {Timeline, TimelineInput} from './types.js';

export type {CaptionWord, Timeline, TimelineInput, TimelineScene, TimelineSceneInput} from './types.js';

const supportedFps = 30;

const assertValidInput = (input: TimelineInput): void => {
  if (input.fps !== supportedFps) {
    throw new Error(`unsupported fps: ${input.fps}`);
  }

  for (const scene of input.scenes) {
    if (!Number.isFinite(scene.audioDurationMs) || scene.audioDurationMs <= 0) {
      throw new Error(`scene ${scene.id} has a non-positive duration`);
    }

    for (const word of scene.words ?? []) {
      if (!Number.isFinite(word.startMs) || !Number.isFinite(word.endMs) || word.endMs <= word.startMs) {
        throw new Error(`caption in scene ${scene.id} has an invalid interval`);
      }
      if (word.startMs < 0 || word.endMs > scene.audioDurationMs) {
        throw new Error(`caption in scene ${scene.id} is outside the scene`);
      }
    }
  }
};

export const buildTimeline = (input: TimelineInput): Timeline => {
  assertValidInput(input);
  let startFrame = 0;
  const scenes = input.scenes.map((scene) => {
    const durationInFrames = Math.ceil(scene.audioDurationMs / 1000 * input.fps);
    const endFrame = startFrame + durationInFrames;
    const timelineScene = scene.words === undefined
      ? {id: scene.id, startFrame, durationInFrames, endFrame}
      : {id: scene.id, startFrame, durationInFrames, endFrame, words: scene.words};
    startFrame = endFrame;
    return timelineScene;
  });

  return {fps: input.fps, scenes};
};
