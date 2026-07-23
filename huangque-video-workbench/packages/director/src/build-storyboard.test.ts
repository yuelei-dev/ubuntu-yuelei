import {describe, expect, it} from 'vitest';
import {buildStoryboard} from './build-storyboard.js';

describe('buildStoryboard', () => {
  it('uses avatar for intro, transition, CTA and visual media for a product demonstration', () => {
    const board = buildStoryboard({
      title: '????',
      script: '?????????????????????????????????????????????????'
    });

    expect(board.scenes.map((scene) => scene.type)).toEqual([
      'avatar',
      'avatar',
      'image_video',
      'avatar',
      'avatar'
    ]);
    expect(board.scenes.every((scene) => scene.type !== 'avatar' || scene.durationEstimate <= 12)).toBe(true);
  });

  it('splits an oversized avatar segment into ordered, valid scenes', () => {
    const board = buildStoryboard({title: '???', script: '?'.repeat(51)});

    expect(board.scenes.map((scene) => scene.id)).toEqual(['scene_001', 'scene_002']);
    expect(board.scenes.map((scene) => scene.order)).toEqual([1, 2]);
    expect(board.scenes.every((scene) => scene.type === 'avatar' && scene.durationEstimate <= 12)).toBe(true);
  });
});
