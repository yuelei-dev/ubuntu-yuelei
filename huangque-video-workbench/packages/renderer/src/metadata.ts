import type {Timeline} from '@huangque/timeline';

export type VerticalKnowledgeMetadata = {
  durationInFrames: number;
  fps: number;
  width: 1080;
  height: 1920;
};

export const calculateVerticalKnowledgeMetadata = ({timeline}: {timeline: Timeline}): VerticalKnowledgeMetadata => ({
  durationInFrames: timeline.scenes.at(-1)?.endFrame ?? 0,
  fps: timeline.fps,
  width: 1080,
  height: 1920
});
