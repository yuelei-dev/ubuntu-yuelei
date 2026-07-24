import type {CaptionWord} from './types.js';

export const activeCaptionAtFrame = (
  words: readonly CaptionWord[],
  frame: number,
  fps: number
): CaptionWord | undefined => {
  const timestampMs = frame / fps * 1000;
  return words
    .map((word, index) => ({word, index}))
    .sort((left, right) => left.word.startMs - right.word.startMs || left.index - right.index)
    .find(({word}) => word.startMs <= timestampMs && timestampMs < word.endMs)
    ?.word;
};
