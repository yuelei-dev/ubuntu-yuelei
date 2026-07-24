import {describe, expect, it} from 'vitest';
import {classifySegment} from './classify-segment.js';
import {segmentScript} from './segment-script.js';

describe('segmentScript', () => {
  it('splits Chinese sentence endings and discards empty segments', () => {
    expect(segmentScript(' 第一段。   第二段？  第三段！ ')).toEqual(['第一段。', '第二段？', '第三段！']);
  });
});

describe('classifySegment', () => {
  it('keeps first and last segments as avatar and classifies interior visual content', () => {
    expect(classifySegment('展示产品', 0, 4)).toBe('avatar');
    expect(classifySegment('数据增长', 1, 4)).toBe('chart');
    expect(classifySegment('产品实拍', 2, 4)).toBe('image_video');
    expect(classifySegment('展示产品', 3, 4)).toBe('avatar');
  });
});
