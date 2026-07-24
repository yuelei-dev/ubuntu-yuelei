import {registerRoot} from 'remotion';
import {RemotionRoot} from './Root';

registerRoot(RemotionRoot);

export {calculateMetadata, RemotionRoot} from './Root';
export {VerticalKnowledgeVideo} from './VerticalKnowledgeVideo';
export {Captions} from './components/Captions';
export {knowledgeTheme} from './themes/knowledge';
export type {RendererScene, VerticalKnowledgeVideoProps} from './types';
