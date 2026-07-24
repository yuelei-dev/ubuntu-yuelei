import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const VisualFullScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="visual full">
  <TitleRegion id={scene.id} text={scene.headline ?? scene.script ?? 'Visual content unavailable'} fontSize={58} lineHeight={1.2}/>
</SceneShell>;
