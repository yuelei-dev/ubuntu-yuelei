import {useCurrentFrame} from 'remotion';
import type {CaptionWord} from '@huangque/timeline';
import {captionPresentation} from '../caption-presentation';
import {knowledgeSafeArea} from '../layout';
import {knowledgeTheme, type KnowledgeTheme} from '../themes/knowledge';

export const Captions = ({
  words,
  fps,
  highlightWords = [],
  theme = knowledgeTheme
}: {
  words: readonly CaptionWord[];
  fps: number;
  highlightWords?: readonly string[];
  theme?: KnowledgeTheme;
}) => {
  const frame = useCurrentFrame();
  const presentation = captionPresentation({words, frame, fps, highlightWords, theme});

  if (presentation.words.length === 0) return null;

  return <div data-testid="captions" style={{
    background: presentation.background,
    borderRadius: 28,
    bottom: 1920 - (knowledgeSafeArea.captions.top + knowledgeSafeArea.captions.height),
    display: '-webkit-box',
    fontSize: 48,
    left: knowledgeSafeArea.left,
    lineHeight: 1.3,
    maxHeight: knowledgeSafeArea.captions.height,
    overflow: 'hidden',
    padding: '20px 28px',
    position: 'absolute',
    right: knowledgeSafeArea.right,
    WebkitBoxOrient: 'vertical',
    WebkitLineClamp: presentation.maxLines
  }}>
    {presentation.words.map((word, index) => <span key={`${word.text}-${index}`} style={{
      color: word.color,
      fontWeight: word.fontWeight,
      marginRight: 12
    }}>{word.text}</span>)}
  </div>;
};
