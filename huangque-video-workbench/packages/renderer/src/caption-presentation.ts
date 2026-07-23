import type {CaptionWord} from '@huangque/timeline';
import {knowledgeSafeArea} from './layout';
import {knowledgeTheme, type KnowledgeTheme} from './themes/knowledge';

export type CaptionPresentation = {
  words: Array<{text: string; color: string; fontWeight: number}>;
  background: string;
  maxLines: number;
};

const wordText = (word: CaptionWord): string => word.text ?? word.word ?? '';

const activeCaptionAtFrame = (words: readonly CaptionWord[], frame: number, fps: number): CaptionWord | undefined => {
  const timestampMs = frame / fps * 1000;
  return words.find((word) => word.startMs <= timestampMs && timestampMs < word.endMs);
};

export const captionPresentation = ({
  words,
  frame,
  fps,
  highlightWords,
  theme = knowledgeTheme
}: {
  words: readonly CaptionWord[];
  frame: number;
  fps: number;
  highlightWords: readonly string[];
  theme?: KnowledgeTheme;
}): CaptionPresentation => {
  const active = activeCaptionAtFrame(words, frame, fps);
  const highlights = new Set(highlightWords);

  return {
    words: words.map((word) => {
      const text = wordText(word);
      return {
        text,
        color: word === active ? theme.primary : theme.text,
        fontWeight: highlights.has(text) ? 800 : 700
      };
    }),
    background: theme.surface,
    maxLines: knowledgeSafeArea.captions.maxLines
  };
};
