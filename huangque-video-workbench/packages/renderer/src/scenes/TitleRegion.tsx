import {knowledgeSafeArea} from '../layout';

export const TitleRegion = ({
  id,
  text,
  fontSize = 68,
  lineHeight = 1.16
}: {
  id: string;
  text: string | undefined;
  fontSize?: number;
  lineHeight?: number;
}) => {
  if (!text) return null;

  return <div data-testid={`title-region-${id}`} style={{
    height: knowledgeSafeArea.title.height,
    left: knowledgeSafeArea.left,
    overflow: 'hidden',
    position: 'absolute',
    right: knowledgeSafeArea.right,
    top: knowledgeSafeArea.title.top
  }}>
    <h1 data-testid={`title-${id}`} style={{
      display: '-webkit-box',
      fontSize,
      lineHeight,
      margin: 0,
      maxHeight: knowledgeSafeArea.title.height,
      overflow: 'hidden',
      WebkitBoxOrient: 'vertical',
      WebkitLineClamp: 2
    }}>{text}</h1>
  </div>;
};
