import {useState} from 'react';
import type {AssetVersion, WorkbenchScene} from '../routes/ProjectDetail';

export const StoryboardCard = ({
  scene, assetVersion, onSave, onRegenerate, regenerating
}: {
  scene: WorkbenchScene;
  assetVersion?: AssetVersion;
  onSave: (script: string) => Promise<void>;
  onRegenerate: () => Promise<void>;
  regenerating: boolean;
}) => {
  const [script, setScript] = useState(scene.script);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const locked = scene.locked === true || scene.visual.locked === true;
  const failureReason = scene.failureReason ?? (typeof scene.visual.failureReason === 'string' ? scene.visual.failureReason : undefined);

  return <article data-testid="storyboard-card" data-scene-id={scene.id} aria-labelledby={`scene-heading-${scene.id}`}>
    <h2 id={`scene-heading-${scene.id}`}>Scene {scene.order}: {scene.id}</h2>
    <p>Lock state: <strong>{locked ? 'Locked' : 'Unlocked'}</strong></p>
    <p>Scene status: <output>{scene.status}</output></p>
    <p>{assetVersion ? `Asset version ${assetVersion.version}` : 'No generated asset version yet'}</p>
    {failureReason && <p role="alert" aria-label={`Scene ${scene.id} failed`}>{failureReason}</p>}
    {error && <p role="alert">{error}</p>}
    <p><label htmlFor={`scene-script-${scene.id}`}>Script for {scene.id}</label>
      <textarea id={`scene-script-${scene.id}`} value={script} disabled={locked || saving} onChange={(event) => setScript(event.target.value)}/></p>
    <button type="button" disabled={locked || saving} onClick={async () => {
      setSaving(true); setError(undefined);
      try { await onSave(script.trim()); } catch { setError('Scene changes could not be saved.'); } finally { setSaving(false); }
    }}>Save {scene.id}</button>
    <button type="button" disabled={regenerating} onClick={onRegenerate}>{regenerating ? `Regenerating ${scene.id}` : `Regenerate ${scene.id}`}</button>
  </article>;
};
