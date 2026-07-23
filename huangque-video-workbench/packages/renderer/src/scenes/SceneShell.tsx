import {AbsoluteFill, Img, Video} from 'remotion';
import type {CSSProperties, ReactNode} from 'react';
import {knowledgeSafeArea, type KnowledgeLayout} from '../layout';
import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';

type SceneShellProps = {
  children?: ReactNode;
  scene: RendererScene;
  theme: KnowledgeTheme;
  allowExternalMedia: boolean;
  variant: string;
};

const usableMediaUri = (uri: string | undefined, allowExternalMedia: boolean): string | undefined => {
  if (!uri) return undefined;
  if (/^https?:/iu.test(uri) && !allowExternalMedia) return undefined;
  return uri;
};

const videoExtensions = /\.(?:m4v|mov|mp4|webm)(?:[?#].*)?$/iu;

const fallbackColorFor = (layout: KnowledgeLayout): string => {
  if (layout.startsWith('avatar_')) return '#14B8A6';
  if (layout.startsWith('visual_')) return '#2563EB';
  if (layout === 'comparison') return '#7C3AED';
  return '#C2410C';
};

const mediaKindFor = (uri: string, layout: KnowledgeLayout, explicitKind?: 'image' | 'video'): 'image' | 'video' => {
  if (explicitKind) return explicitKind;
  if (videoExtensions.test(uri) || layout.startsWith('avatar_')) return 'video';
  return 'image';
};

export const SceneMedia = ({
  uri,
  layout,
  mediaKind,
  allowExternalMedia,
  testId,
  style
}: {
  uri: string | undefined;
  layout: KnowledgeLayout;
  mediaKind?: 'image' | 'video';
  allowExternalMedia: boolean;
  testId: string;
  style: CSSProperties;
}) => {
  const mediaUri = usableMediaUri(uri, allowExternalMedia);
  if (!mediaUri) return null;

  if (mediaKindFor(mediaUri, layout, mediaKind) === 'video') {
    return <Video src={mediaUri} data-testid={testId} style={style}/>;
  }
  return <Img src={mediaUri} data-testid={testId} style={style}/>;
};

export const SceneShell = ({children, scene, theme, allowExternalMedia, variant}: SceneShellProps) => {
  const hasMedia = Boolean(usableMediaUri(scene.assetUri, allowExternalMedia));
  return <AbsoluteFill
    data-testid={`scene-${scene.id}`}
    style={{
      backgroundColor: hasMedia ? theme.background : fallbackColorFor(scene.layout),
      color: theme.text,
      overflow: 'hidden'
    }}
  >
    <SceneMedia
      uri={scene.assetUri}
      layout={scene.layout}
      mediaKind={scene.assetMediaKind}
      allowExternalMedia={allowExternalMedia}
      testId={`media-${scene.id}`}
      style={{height: '100%', objectFit: 'cover', opacity: 0.56, position: 'absolute', width: '100%'}}
    />
    {!hasMedia && <div data-testid={`fallback-${scene.id}`} hidden/>}
    <div data-testid={`safe-area-${scene.id}`} aria-label={`${variant} scene fallback`} style={{
      display: 'flex', flexDirection: 'column', gap: 24, height: '100%',
      paddingLeft: knowledgeSafeArea.left, paddingRight: knowledgeSafeArea.right,
      position: 'relative', zIndex: 1
    }}>
      {children}
    </div>
  </AbsoluteFill>;
};
