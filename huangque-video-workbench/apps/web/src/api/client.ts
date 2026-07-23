import type {CreateProjectInput} from '../routes/NewProject';
import type {ProjectDetailApi, ProjectEventSubscription, WorkbenchProject} from '../routes/ProjectDetail';
import {parseWorkbenchProject} from './project-schema';

type FetchResponse = {ok: boolean; status?: number; json(): Promise<unknown>};
type Fetcher = (input: string, init: {method: string; headers?: Record<string, string>; body?: string}) => Promise<FetchResponse>;
type EventSourceLike = {
  onmessage: ((event: {data: string}) => void) | null;
  onerror: (() => void) | null;
  addEventListener?: (name: string, listener: (event: {data: string}) => void) => void;
  removeEventListener?: (name: string, listener: (event: {data: string}) => void) => void;
  close(): void;
};
type CreateEventSource = (url: string) => EventSourceLike;

export type WorkbenchApi = Omit<ProjectDetailApi, 'getProject' | 'updateScene' | 'renderProject' | 'subscribeToProjectEvents'> & {
  createProject(input: CreateProjectInput): Promise<{id: string}>;
  getProject(projectId: string): Promise<WorkbenchProject>;
  updateScene(projectId: string, sceneId: string, patch: {script: string}): Promise<unknown>;
  renderProject(projectId: string): Promise<unknown>;
  subscribeToProjectEvents(projectId: string, onProject: (project: unknown) => void, onError: () => void): ProjectEventSubscription;
};

export type ProjectApiDependencies = {
  baseUrl?: string;
  fetcher?: Fetcher;
  createEventSource?: CreateEventSource;
};

export const createProjectApi = ({
  baseUrl = '',
  fetcher = globalThis.fetch.bind(globalThis) as unknown as Fetcher,
  createEventSource = (url) => new EventSource(url) as unknown as EventSourceLike
}: ProjectApiDependencies = {}): WorkbenchApi => {
  const path = (suffix: string) => `${baseUrl}${suffix}`;
  const request = async (suffix: string, method: string, payload?: unknown): Promise<unknown> => {
    const response = await fetcher(path(suffix), payload === undefined
      ? {method}
      : {method, headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)});
    if (!response.ok) throw new Error(`project API request failed${response.status ? ` (${response.status})` : ''}`);
    return response.json();
  };
  const projectPath = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}`;

  return {
    async createProject(input) {
      const response = await request('/api/projects', 'POST', input);
      if (!response || typeof response !== 'object' || typeof (response as {id?: unknown}).id !== 'string') throw new Error('malformed project response');
      return response as {id: string};
    },
    async getProject(projectId) {
      const response = await request(projectPath(projectId), 'GET');
      const project = parseWorkbenchProject(response);
      if (!project) throw new Error('malformed project response');
      return project;
    },
    updateScene(projectId, sceneId, patch) {
      return request(`${projectPath(projectId)}/scenes/${encodeURIComponent(sceneId)}`, 'PATCH', patch);
    },
    regenerateScene(projectId, sceneId) {
      return request(`${projectPath(projectId)}/scenes/${encodeURIComponent(sceneId)}/regenerate`, 'POST');
    },
    renderProject(projectId) {
      return request(`${projectPath(projectId)}/render`, 'POST');
    },
    subscribeToProjectEvents(projectId, onProject, onError) {
      const source = createEventSource(path(`${projectPath(projectId)}/events`));
      const receive = (event: {data: string}) => {
        try {
          onProject(parseWorkbenchProject(JSON.parse(event.data) as unknown));
        } catch {
          onProject(undefined);
        }
      };
      if (source.addEventListener) source.addEventListener('project', receive);
      else source.onmessage = receive;
      source.onerror = onError;
      return {close: () => { source.removeEventListener?.('project', receive); source.close(); }};
    }
  };
};
