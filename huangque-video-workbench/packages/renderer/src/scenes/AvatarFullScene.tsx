import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const AvatarFullScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="avatar full">
  <TitleRegion id={scene.id} text={scene.headline}/>
  <div style={{alignItems: 'center', display: 'flex', flex: 1, justifyContent: 'center'}}>
    <div style={{
      alignItems: 'center', background: theme.primary, borderRadius: '50%', display: 'flex', height: 540,
      justifyContent: 'center', width: 540
    }}>
      <span style={{color: theme.background, fontSize: 72, fontWeight: 800}}>AVATAR</span>
    </div>
  </div>
</SceneShell>;
