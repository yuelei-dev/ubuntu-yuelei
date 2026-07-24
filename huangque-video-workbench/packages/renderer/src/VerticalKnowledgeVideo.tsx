import {Sequence} from 'remotion';
import {Captions} from './components/Captions';
import {assertKnowledgeLayout} from './layout';
import {sceneSequences} from './scene-sequences';
import {AvatarFullScene} from './scenes/AvatarFullScene';
import {AvatarPipScene} from './scenes/AvatarPipScene';
import {AvatarTitleScene} from './scenes/AvatarTitleScene';
import {ComparisonScene} from './scenes/ComparisonScene';
import {TextFallbackScene} from './scenes/TextFallbackScene';
import {VisualCardScene} from './scenes/VisualCardScene';
import {VisualFullScene} from './scenes/VisualFullScene';
import {knowledgeTheme} from './themes/knowledge';
import type {RendererScene, VerticalKnowledgeVideoProps} from './types';

const fallbackScene = (id: string): RendererScene => ({
  id,
  layout: 'text_fallback',
  script: 'The requested scene data is unavailable.'
});

const SceneLayout = ({scene, ...props}: {
  scene: RendererScene;
  theme: typeof knowledgeTheme;
  allowExternalMedia: boolean;
}) => {
  assertKnowledgeLayout(scene.layout);

  switch (scene.layout) {
    case 'avatar_full': return <AvatarFullScene scene={scene} {...props}/>;
    case 'avatar_title': return <AvatarTitleScene scene={scene} {...props}/>;
    case 'avatar_pip': return <AvatarPipScene scene={scene} {...props}/>;
    case 'visual_full': return <VisualFullScene scene={scene} {...props}/>;
    case 'visual_card': return <VisualCardScene scene={scene} {...props}/>;
    case 'comparison': return <ComparisonScene scene={scene} {...props}/>;
    case 'text_fallback': return <TextFallbackScene scene={scene} {...props}/>;
  }
};

export const VerticalKnowledgeVideo = ({
  timeline,
  scenes,
  theme = knowledgeTheme,
  allowExternalMedia = false
}: VerticalKnowledgeVideoProps) => {
  const scenesById = new Map(scenes.map((scene) => [scene.id, scene]));

  return <>
    {sceneSequences(timeline).map((sequence) => {
      const timelineScene = timeline.scenes.find((scene) => scene.id === sequence.sceneId);
      const scene = scenesById.get(sequence.sceneId) ?? fallbackScene(sequence.sceneId);

      return <Sequence key={sequence.sceneId} from={sequence.from} durationInFrames={sequence.durationInFrames}>
        <SceneLayout scene={scene} theme={theme} allowExternalMedia={allowExternalMedia}/>
        <Captions
          words={timelineScene?.words ?? []}
          fps={timeline.fps}
          highlightWords={scene.highlightWords}
          theme={theme}
        />
      </Sequence>;
    })}
  </>;
};
