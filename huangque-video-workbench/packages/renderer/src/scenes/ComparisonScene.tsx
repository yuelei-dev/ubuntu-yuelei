import type {KnowledgeTheme} from '../themes/knowledge';
import type {RendererScene} from '../types';
import {SceneMedia, SceneShell} from './SceneShell';
import {TitleRegion} from './TitleRegion';

export const ComparisonScene = ({scene, theme, allowExternalMedia}: {
  scene: RendererScene; theme: KnowledgeTheme; allowExternalMedia: boolean;
}) => <SceneShell scene={scene} theme={theme} allowExternalMedia={allowExternalMedia} variant="comparison">
  <TitleRegion id={scene.id} text={scene.headline}/>
  <div style={{display: 'flex', flex: 1, gap: 28, paddingTop: 420}}>
    {['Before', 'After'].map((label, index) => <div key={label} style={{
      background: index === 1 ? theme.primary : 'rgba(0, 0, 0, 0.52)', borderRadius: 32, flex: 1, padding: 36
    }}>
      <h2 style={{color: index === 1 ? theme.background : theme.text, fontSize: 48}}>{label}</h2>
      <p style={{color: index === 1 ? theme.background : theme.text, fontSize: 38, lineHeight: 1.3}}>{scene.script ?? 'Comparison content'}</p>
      {index === 1 && <SceneMedia
        uri={scene.comparisonAssetUri}
        layout={scene.layout}
        mediaKind={scene.comparisonAssetMediaKind}
        allowExternalMedia={allowExternalMedia}
        testId={`comparison-media-${scene.id}`}
        style={{borderRadius: 20, height: 180, objectFit: 'cover', width: '100%'}}
      />}
    </div>)}
  </div>
</SceneShell>;
