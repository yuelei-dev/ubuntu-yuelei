import type {Timeline} from '@huangque/timeline';

export type SceneSequence = {
  sceneId: string;
  from: number;
  durationInFrames: number;
};

export const sceneSequences = (timeline: Timeline): SceneSequence[] => timeline.scenes.map((scene) => ({
  sceneId: scene.id,
  from: scene.startFrame,
  durationInFrames: scene.durationInFrames
}));
