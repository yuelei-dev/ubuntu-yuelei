export type CaptionWord = {
  text?: string;
  word?: string;
  startMs: number;
  endMs: number;
};

export type TimelineSceneInput = {
  id: string;
  audioDurationMs: number;
  words?: CaptionWord[];
};

export type TimelineInput = {
  fps: number;
  scenes: TimelineSceneInput[];
};

export type TimelineScene = {
  id: string;
  startFrame: number;
  durationInFrames: number;
  endFrame: number;
  words?: CaptionWord[];
};

export type Timeline = {
  fps: number;
  scenes: TimelineScene[];
};
