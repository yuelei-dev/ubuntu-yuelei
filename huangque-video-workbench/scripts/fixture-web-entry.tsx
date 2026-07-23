import {useEffect, useMemo, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {createProjectApi, NewProject, ProjectDetail, type WorkbenchProject} from '@huangque/web';

const projectIdFromPath = (path: string): string | undefined => {
  const match = /^\/projects\/([A-Za-z0-9_-]+)$/u.exec(path);
  return match?.[1];
};

const FixtureWorkbench = () => {
  const api = useMemo(() => createProjectApi(), []);
  const [path, setPath] = useState(window.location.pathname);
  const [project, setProject] = useState<WorkbenchProject>();
  const [error, setError] = useState<string>();
  const projectId = projectIdFromPath(path);

  useEffect(() => {
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let active = true;
    setProject(undefined);
    setError(undefined);
    void api.getProject(projectId)
      .then((next) => { if (active) setProject(next); })
      .catch(() => { if (active) setError('Project could not be loaded.'); });
    return () => { active = false; };
  }, [api, projectId]);

  if (path === '/' || path === '/projects/new') {
    return <NewProject api={api} onCreated={(createdProjectId) => {
      const nextPath = `/projects/${createdProjectId}`;
      window.history.pushState({}, '', nextPath);
      setPath(nextPath);
    }}/>;
  }
  if (!projectId) return <main><p role="alert">Page not found.</p></main>;
  if (error) return <main><p role="alert">{error}</p></main>;
  if (!project) return <main><p role="status">Loading project.</p></main>;
  return <ProjectDetail api={api} project={project}/>;
};

const root = document.getElementById('root');
if (!root) throw new Error('fixture web root is missing');
createRoot(root).render(<FixtureWorkbench/>);
