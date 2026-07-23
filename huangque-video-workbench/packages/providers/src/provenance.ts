type SceneWithProvenance = {
  type: string;
  asset?: {factual?: boolean} | null;
};

type AssetWithProvenance = {
  provenance: 'uploaded' | 'enterprise' | 'licensed' | 'generated' | 'fallback';
};

export const assertAllowedProvenance = (scene: SceneWithProvenance, asset: AssetWithProvenance): void => {
  const isFactualEvidence = scene.asset?.factual === true && (scene.type === 'screenshot' || scene.type === 'upload');
  if (isFactualEvidence && (asset.provenance === 'generated' || asset.provenance === 'fallback')) {
    throw new Error('factual scenes require uploaded or verified assets');
  }
};
