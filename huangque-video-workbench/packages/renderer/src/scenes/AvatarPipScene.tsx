import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const AvatarPipScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="avatar picture in picture">
  <TitleRegion id={scene.id} text={scene.headline ?? scene.script ?? 'Key point'}/>
  <div style={{alignSelf: 'flex-end', background: theme.primary, border: `10px solid ${theme.text}`, borderRadius: '50%', height: 250, marginBottom: 510, width: 250}}/>
</SceneShell>;
