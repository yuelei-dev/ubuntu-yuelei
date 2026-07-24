import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const TextFallbackScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="text fallback">
  <TitleRegion id={scene.id} text={scene.headline ?? 'Continue watching'} fontSize={76} lineHeight={1.15}/>
  <div style={{marginTop: 420}}>
    <p style={{fontSize: 48, lineHeight: 1.4}}>{scene.script ?? 'The requested media was unavailable, so the narration remains available as text.'}</p>
  </div>
</SceneShell>;
