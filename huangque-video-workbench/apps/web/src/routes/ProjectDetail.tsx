import {useEffect, useRef, useState} from 'react';
import {ProjectProgress} from '../components/ProjectProgress';
import {StoryboardCard} from '../components/StoryboardCard';
import {VideoPreview} from '../components/VideoPreview';
import {parseWorkbenchProject, type AssetVersion, type WorkbenchProject, type WorkbenchScene} from '../api/project-schema';

export type {AssetVersion, WorkbenchProject, WorkbenchScene} from '../api/project-schema';

export type ProjectEventSubscription = {close(): void};
export type ProjectDetailApi = {
  getProject?: (projectId: string) => Promise<WorkbenchProject>;
  updateScene?: (projectId: string, sceneId: string, patch: {script: string}) => Promise<unknown>;
  regenerateScene(projectId: string, sceneId: string): Promise<unknown>;
  renderProject?: (projectId: string) => Promise<unknown>;
  subscribeToProjectEvents?: (
    projectId: string,
    onProject: (project: unknown) => void,
    onError: () => void
  ) => ProjectEventSubscription;
};

const terminalStatuses = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'NEEDS_USER_INPUT']);
const isProject = (value: unknown): value is WorkbenchProject => parseWorkbenchProject(value) !== undefined;

export const ProjectDetail = ({api, project}: {api: ProjectDetailApi; project: WorkbenchProject | null | undefined}) => {
  const [currentProject, setCurrentProject] = useState<WorkbenchProject | null | undefined>(project);
  const [polling, setPolling] = useState(false);
  const [regenerating, setRegenerating] = useState<string>();
  const [rendering, setRendering] = useState(false);
  const [actionError, setActionError] = useState<string>();
  const disconnects = useRef(0);

  useEffect(() => { setCurrentProject(project); setPolling(false); disconnects.current = 0; }, [project]);
  useEffect(() => {
    if (!isProject(currentProject) || terminalStatuses.has(currentProject.status)) return;
    const projectId = currentProject.id;
    if (polling) {
      if (!api.getProject) return;
      const poll = () => api.getProject!(projectId).then((next) => { if (isProject(next)) setCurrentProject(next); })
        .catch(() => setActionError('Project status could not be refreshed.'));
      const timer = window.setInterval(poll, 5_000);
      return () => window.clearInterval(timer);
    }
    if (!api.subscribeToProjectEvents) return;
    const subscription = api.subscribeToProjectEvents(projectId, (next) => {
      const parsed = parseWorkbenchProject(next);
      if (parsed) setCurrentProject(parsed);
      else setActionError('Project update data is unavailable.');
    }, () => {
      disconnects.current += 1;
      if (disconnects.current >= 2) setPolling(true);
    });
    return () => subscription.close();
  }, [api, currentProject, polling]);

  if (!isProject(currentProject)) return <main><p role="alert">Project data is unavailable.</p></main>;
  const orderedScenes = currentProject.scenes.slice().sort((left, right) => left.order - right.order);
  const latestAssetFor = (sceneId: string) => currentProject.assetVersions
    .filter((asset) => asset.sceneId === sceneId)
    .sort((left, right) => right.version - left.version)[0];

  return <main>
    <h1>{currentProject.title}</h1>
    <ProjectProgress status={currentProject.status}/>
    {actionError && <p role="alert">{actionError}</p>}
    <section aria-labelledby="storyboard-heading">
      <h2 id="storyboard-heading">Storyboard</h2>
      {orderedScenes.length === 0 ? <p>No storyboard scenes are available yet.</p> : orderedScenes.map((scene) => <StoryboardCard
        key={scene.id}
        scene={scene}
        assetVersion={latestAssetFor(scene.id)}
        regenerating={regenerating === scene.id}
        onSave={async (script) => {
          if (!api.updateScene) throw new Error('Scene editing is unavailable');
          await api.updateScene(currentProject.id, scene.id, {script});
          setCurrentProject((value) => isProject(value) ? {...value, scenes: value.scenes.map((candidate) => candidate.id === scene.id ? {...candidate, script} : candidate)} : value);
        }}
        onRegenerate={async () => {
          setRegenerating(scene.id); setActionError(undefined);
          try { await api.regenerateScene(currentProject.id, scene.id); }
          catch { setActionError(`Scene ${scene.id} could not be regenerated.`); }
          finally { setRegenerating(undefined); }
        }}
      />)}</section>
    <VideoPreview previewUrl={currentProject.previewUrl}/>
    <section aria-labelledby="export-heading">
      <h2 id="export-heading">Export</h2>
      {currentProject.downloadUrl && <a href={currentProject.downloadUrl} download>Download final video</a>}
      {api.renderProject && <button type="button" disabled={rendering} onClick={async () => {
        setRendering(true); setActionError(undefined);
        try { await api.renderProject!(currentProject.id); } catch { setActionError('Final render could not be started.'); } finally { setRendering(false); }
      }}>{rendering ? 'Starting final render' : 'Render final video'}</button>}
    </section>
  </main>;
};
