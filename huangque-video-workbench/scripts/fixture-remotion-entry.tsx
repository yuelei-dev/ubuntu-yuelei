import {Composition, registerRoot} from 'remotion';
import {FixtureCanvasVideo} from './FixtureCanvasVideo';
import type {VerticalKnowledgeVideoProps} from '../packages/renderer/src/types.js';

const defaultProps: VerticalKnowledgeVideoProps = {
  timeline: {fps: 30, scenes: [{id: 'fallback', startFrame: 0, durationInFrames: 30, endFrame: 30}]},
  scenes: [{id: 'fallback', layout: 'text_fallback', script: 'Fixture preview'}]
};

const Root = () => <Composition
  id="FixtureCanvasVideo"
  component={FixtureCanvasVideo}
  defaultProps={defaultProps}
  durationInFrames={30}
  fps={30}
  width={1080}
  height={1920}
  calculateMetadata={({props}) => ({
    durationInFrames: props.timeline.scenes.at(-1)?.endFrame ?? 30,
    fps: props.timeline.fps,
    width: 1080,
    height: 1920
  })}
/>;

registerRoot(Root);
