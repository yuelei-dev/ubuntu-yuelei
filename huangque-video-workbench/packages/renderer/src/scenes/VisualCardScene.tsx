import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const VisualCardScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="visual card">
  <TitleRegion id={scene.id} text={scene.headline ?? 'Knowledge card'}/>
  <div style={{background: 'rgba(0, 0, 0, 0.52)', borderRadius: 36, marginTop: 420, padding: 52}}>
    <p style={{fontSize: 42, lineHeight: 1.35, marginBottom: 0}}>{scene.script ?? 'Media is unavailable; this deterministic fallback preserves the narration.'}</p>
  </div>
</SceneShell>;
