import {describe, expect, it} from 'vitest';
import {validateTimeline} from './validate-timeline';

describe('validateTimeline', () => {
  it('reports unsupported fps, gaps, overlaps, non-positive durations, and inconsistent scene bounds', () => {
    const errors = validateTimeline({
      fps: 24,
      scenes: [
        {id: 's1', startFrame: 0, durationInFrames: 30, endFrame: 30},
        {id: 's2', startFrame: 32, durationInFrames: 0, endFrame: 35},
        {id: 's3', startFrame: 34, durationInFrames: 10, endFrame: 44}
      ]
    });

    expect(errors).toContain('unsupported fps: 24');
    expect(errors).toContain('scene s2 has a gap before frame 32');
    expect(errors).toContain('scene s2 has a non-positive duration');
    expect(errors).toContain('scene s2 has inconsistent end frame');
    expect(errors).toContain('scene s3 overlaps the previous scene at frame 34');
  });

  it('reports caption timestamps outside a scene and invalid word intervals', () => {
    const errors = validateTimeline({
      fps: 30,
      scenes: [{
        id: 's1',
        startFrame: 0,
        durationInFrames: 30,
        endFrame: 30,
        words: [
          {text: 'early', startMs: -1, endMs: 100},
          {text: 'backwards', startMs: 500, endMs: 500},
          {text: 'late', startMs: 900, endMs: 1100}
        ]
      }]
    });

    expect(errors).toContain('caption early in scene s1 is outside the scene');
    expect(errors).toContain('caption backwards in scene s1 has an invalid interval');
    expect(errors).toContain('caption late in scene s1 is outside the scene');
  });
});
