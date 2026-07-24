import type {SceneJob, SceneJobProcessor} from './registry.js';

type FactualScene = {
  type: string;
  asset?: {factual?: boolean} | null;
  assetVersions: unknown[];
};

/** Factual evidence must wait for a real supplied asset; it is never generated. */
export const isMissingFactualAsset = (scene: FactualScene): boolean =>
  scene.asset?.factual === true && (scene.type === 'screenshot' || scene.type === 'upload') && scene.assetVersions.length === 0;

export const createAssetGenerateProcessor = (
  generate: (job: SceneJob, kind: 'asset') => Promise<void>
): SceneJobProcessor => ({
  process: (job) => generate(job, 'asset')
});
