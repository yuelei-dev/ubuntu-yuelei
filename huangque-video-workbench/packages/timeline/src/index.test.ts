import {expect, it} from 'vitest';
import {activeCaptionAtFrame, buildTimeline, validateTimeline} from './index';

it('exports the timeline builder, validator, and caption lookup', () => {
  const timeline = buildTimeline({fps: 30, scenes: [{id: 's1', audioDurationMs: 1000}]});

  expect(validateTimeline(timeline)).toEqual([]);
  expect(activeCaptionAtFrame([], 0, 30)).toBeUndefined();
});
