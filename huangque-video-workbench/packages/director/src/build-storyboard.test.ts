import {describe, expect, it} from 'vitest';
import {buildStoryboard} from './build-storyboard.js';

describe('buildStoryboard', () => {
  it('uses avatar for intro, transition, CTA and visual media for a product demonstration', () => {
    const board = buildStoryboard({
      title: '产品介绍',
      script: '大家好，我是子墨。这一个产品有三个特点。先看一下产品实拍。第一个特点是使用简单。点击关注，下期见。'
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
    const board = buildStoryboard({title: '长文案', script: '很'.repeat(51)});

    expect(board.scenes.map((scene) => scene.id)).toEqual(['scene_001', 'scene_002']);
    expect(board.scenes.map((scene) => scene.order)).toEqual([1, 2]);
    expect(board.scenes.every((scene) => scene.type === 'avatar' && scene.durationEstimate <= 12)).toBe(true);
  });

  it('accepts 100 scenes and explicitly rejects the 101st', () => {
    const sentence = 'One concise sentence.';
    expect(buildStoryboard({title: 'limit', script: Array(100).fill(sentence).join('。')}).scenes).toHaveLength(100);
    expect(() => buildStoryboard({title: 'over', script: Array(101).fill(sentence).join('。')}))
      .toThrow('maximum is 100');
  });
});
