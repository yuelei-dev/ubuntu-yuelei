import {describe, expect, it} from 'vitest';
import {classifySegment} from './classify-segment.js';
import {segmentScript} from './segment-script.js';

describe('segmentScript', () => {
  it('splits Chinese sentence endings and discards empty segments', () => {
    expect(segmentScript(' ????   ????  ???? ')).toEqual(['????', '????', '????']);
  });
});

describe('classifySegment', () => {
  it('keeps first and last segments as avatar and classifies interior visual content', () => {
    expect(classifySegment('????', 0, 4)).toBe('avatar');
    expect(classifySegment('????', 1, 4)).toBe('chart');
    expect(classifySegment('????', 2, 4)).toBe('image_video');
    expect(classifySegment('????', 3, 4)).toBe('avatar');
  });
});
