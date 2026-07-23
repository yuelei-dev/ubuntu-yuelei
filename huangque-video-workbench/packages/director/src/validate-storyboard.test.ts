import {describe, expect, it} from 'vitest';
import type {Storyboard} from '@huangque/contracts';
import {validateStoryboard} from './validate-storyboard.js';

const project = {title: '??', width: 1080 as const, height: 1920 as const, fps: 30 as const};

describe('validateStoryboard', () => {
  it('reports an avatar scene exceeding 12 seconds', () => {
    const board = {
      project,
      scenes: [{
        id: 'scene_001', order: 1, type: 'avatar' as const, purpose: 'narration', script: '??',
        durationEstimate: 12.1, visual: {layout: 'avatar_full', highlightWords: []}
      }]
    } as Storyboard;

    expect(validateStoryboard(board)).toContain('scene_001: avatar scene exceeds 12 seconds');
  });

  it('reports generated media requested for factual screenshots or uploads', () => {
    const board = {
      project,
      scenes: [{
        id: 'scene_001', order: 1, type: 'screenshot' as const, purpose: 'evidence', script: '????',
        durationEstimate: 3, visual: {layout: 'visual_full', highlightWords: []},
        asset: {source: 'generate', factual: true}
      }]
    } as Storyboard;

    expect(validateStoryboard(board)).toContain('scene_001: factual screenshot/upload scene cannot request generated media');
  });

  it('reports more than 15 seconds without a visual-type scene', () => {
    const board = {
      project,
      scenes: [
        {id: 'scene_001', order: 1, type: 'avatar' as const, purpose: 'narration', script: '???', durationEstimate: 8, visual: {layout: 'avatar_full', highlightWords: []}},
        {id: 'scene_002', order: 2, type: 'avatar' as const, purpose: 'narration', script: '???', durationEstimate: 8, visual: {layout: 'avatar_full', highlightWords: []}}
      ]
    } as Storyboard;

    expect(validateStoryboard(board)).toContain('scene_002: more than 15 seconds without a visual-type scene');
  });
});
