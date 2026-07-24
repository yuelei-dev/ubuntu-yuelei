import type {Scene} from '@huangque/contracts';
import type {Timeline} from '@huangque/timeline';
import type {KnowledgeLayout} from './layout';
import type {KnowledgeTheme} from './themes/knowledge';

export type RendererScene = {
  id: Scene['id'];
  layout: KnowledgeLayout;
  script?: Scene['script'];
  headline?: NonNullable<Scene['visual']['headline']>;
  highlightWords?: Scene['visual']['highlightWords'];
  assetUri?: string;
  assetMediaKind?: 'image' | 'video';
  comparisonAssetUri?: string;
  comparisonAssetMediaKind?: 'image' | 'video';
};

export type VerticalKnowledgeVideoProps = {
  timeline: Timeline;
  scenes: RendererScene[];
  theme?: KnowledgeTheme;
  allowExternalMedia?: boolean;
};
