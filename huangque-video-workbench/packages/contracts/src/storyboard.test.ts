import {describe, expect, it} from 'vitest';
import {StoryboardSchema} from './storyboard';

describe('StoryboardSchema', () => {
  it('rejects an avatar scene longer than 12 seconds', () => {
    const result = StoryboardSchema.safeParse({
      project: {title: '??', width: 1080, height: 1920, fps: 30},
      scenes: [{id: 's1', order: 1, type: 'avatar', purpose: 'intro', script: '??', durationEstimate: 13, visual: {layout: 'avatar_full', highlightWords: []}}]
    });
    expect(result.success).toBe(false);
  });
});
