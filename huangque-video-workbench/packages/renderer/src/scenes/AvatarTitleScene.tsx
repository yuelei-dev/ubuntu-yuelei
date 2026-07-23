import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const AvatarTitleScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="avatar title">
  <TitleRegion id={scene.id} text={scene.headline ?? scene.script ?? 'Knowledge highlight'} fontSize={76} lineHeight={1.14}/>
  <div style={{alignItems: 'center', display: 'flex', flex: 1, justifyContent: 'center'}}>
    <div style={{background: theme.primary, borderRadius: '50%', height: 420, opacity: 0.92, width: 420}}/>
  </div>
</SceneShell>;
