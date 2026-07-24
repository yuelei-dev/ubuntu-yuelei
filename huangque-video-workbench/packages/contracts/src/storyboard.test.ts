import {describe, expect, it} from 'vitest';
import {StoryboardSchema} from './storyboard';

describe('StoryboardSchema', () => {
  it('rejects an avatar scene longer than 12 seconds', () => {
    const result = StoryboardSchema.safeParse({
      project: {title: '测试', width: 1080, height: 1920, fps: 30},
      scenes: [{id: 's1', order: 1, type: 'avatar', purpose: 'intro', script: '你好', durationEstimate: 13, visual: {layout: 'avatar_full', highlightWords: []}}]
    });
    expect(result.success).toBe(false);
  });

  it('enforces bounded editable scene strings and arrays', async () => {
    const {EditableScenePatchSchema} = await import('./storyboard');
    expect(EditableScenePatchSchema.safeParse({
      script: 'x'.repeat(10_000), visualPrompt: 'p'.repeat(4_000),
      visual: {layout: 'l'.repeat(64), headline: 'h'.repeat(200), highlightWords: Array(20).fill('word')}
    }).success).toBe(true);
    expect(EditableScenePatchSchema.safeParse({script: 'x'.repeat(10_001)}).success).toBe(false);
    expect(EditableScenePatchSchema.safeParse({visual: {highlightWords: Array(21).fill('word')}}).success).toBe(false);
  });
});
