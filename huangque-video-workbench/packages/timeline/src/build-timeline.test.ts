import {describe, expect, it} from 'vitest';
import {buildTimeline} from './build-timeline';

describe('buildTimeline', () => {
  it('uses exact audio duration and contiguous frames', () => {
    const timeline = buildTimeline({fps: 30, scenes: [{id: 's1', audioDurationMs: 3210}, {id: 's2', audioDurationMs: 1990}]});

    expect(timeline.scenes).toEqual([
      {id: 's1', startFrame: 0, durationInFrames: 97, endFrame: 97},
      {id: 's2', startFrame: 97, durationInFrames: 60, endFrame: 157}
    ]);
  });

  it('rejects unsupported frame rates and non-positive audio durations', () => {
    expect(() => buildTimeline({fps: 24, scenes: [{id: 's1', audioDurationMs: 1000}]})).toThrow(/unsupported fps/i);
    expect(() => buildTimeline({fps: 30, scenes: [{id: 's1', audioDurationMs: 0}]})).toThrow(/duration/i);
  });

  it('rejects captions outside the source scene duration', () => {
    expect(() => buildTimeline({
      fps: 30,
      scenes: [{
        id: 's1',
        audioDurationMs: 1000,
        words: [{text: 'late', startMs: 950, endMs: 1100}]
      }]
    })).toThrow(/caption/i);
  });
});
