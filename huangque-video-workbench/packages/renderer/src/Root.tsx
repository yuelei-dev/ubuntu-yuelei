import {Composition} from 'remotion';
import {VerticalKnowledgeVideo} from './VerticalKnowledgeVideo';
import {calculateVerticalKnowledgeMetadata} from './metadata';
import type {VerticalKnowledgeVideoProps} from './types';

const defaultProps: VerticalKnowledgeVideoProps = {
  timeline: {fps: 30, scenes: [{id: 'fallback', startFrame: 0, durationInFrames: 30, endFrame: 30}]},
  scenes: [{id: 'fallback', layout: 'text_fallback', script: 'Add project data to render a preview.'}]
};

export const calculateMetadata = ({props}: {props: VerticalKnowledgeVideoProps}) =>
  calculateVerticalKnowledgeMetadata({timeline: props.timeline});

export const RemotionRoot = () => <Composition
  id="VerticalKnowledgeVideo"
  component={VerticalKnowledgeVideo}
  defaultProps={defaultProps}
  durationInFrames={30}
  fps={30}
  width={1080}
  height={1920}
  calculateMetadata={calculateMetadata}
/>;
