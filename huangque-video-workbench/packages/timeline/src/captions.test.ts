import {describe, expect, it} from 'vitest';
import {activeCaptionAtFrame} from './captions';

describe('activeCaptionAtFrame', () => {
  it('selects exactly the next word at a shared boundary frame', () => {
    const words = [
      {text: 'first', startMs: 0, endMs: 100},
      {text: 'second', startMs: 100, endMs: 200}
    ];

    expect(activeCaptionAtFrame(words, 2, 30)).toMatchObject({text: 'first'});
    expect(activeCaptionAtFrame(words, 3, 30)).toMatchObject({text: 'second'});
  });

  it('returns no caption outside every word interval', () => {
    expect(activeCaptionAtFrame([{text: 'only', startMs: 0, endMs: 100}], 3, 30)).toBeUndefined();
  });
});
