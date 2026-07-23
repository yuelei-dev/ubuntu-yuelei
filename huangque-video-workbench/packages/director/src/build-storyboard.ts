import {StoryboardSchema, type Scene, type Storyboard} from '@huangque/contracts';
import {classifySegment} from './classify-segment.js';
import {segmentScript} from './segment-script.js';

const MAX_AVATAR_CHARACTERS = Math.floor(12 * 4.2);

const estimateDuration = (text: string): number => Math.max(2, text.length / 4.2);

const splitAvatarSegment = (text: string): string[] => {
  if (estimateDuration(text) <= 12) return [text];

  return Array.from({length: Math.ceil(text.length / MAX_AVATAR_CHARACTERS)}, (_, index) =>
    text.slice(index * MAX_AVATAR_CHARACTERS, (index + 1) * MAX_AVATAR_CHARACTERS)
  );
};

const visualFor = (type: Scene['type']): Scene['visual'] => ({
  layout: type === 'avatar' ? 'avatar_full' : 'visual_full',
  highlightWords: []
});

export const buildStoryboard = (input: {title: string; script: string}): Storyboard => {
  const segments = segmentScript(input.script);
  const scenes = segments
    .flatMap((segment, index) => {
      const type = classifySegment(segment, index, segments.length);
      const scripts = type === 'avatar' ? splitAvatarSegment(segment) : [segment];

      return scripts.map((script) => ({type, script}));
    })
    .map(({type, script}, index) => ({
      id: `scene_${String(index + 1).padStart(3, '0')}`,
      order: index + 1,
      type,
      purpose: type === 'avatar' ? 'narration' : 'visual_support',
      script,
      durationEstimate: estimateDuration(script),
      visual: visualFor(type)
    }));

  return StoryboardSchema.parse({
    project: {title: input.title, width: 1080, height: 1920, fps: 30},
    scenes
  });
};
