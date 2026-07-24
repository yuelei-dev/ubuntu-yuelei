import {describe, expect, it, vi} from 'vitest';
import {createProjectApi} from './client';

describe('createProjectApi', () => {
  it('uses injected fetch for project mutations and injected EventSource for SSE', async () => {
    const fetcher = vi.fn().mockResolvedValue({ok: true, json: async () => ({id: 'project_1'})});
    const source = {close: vi.fn()};
    const createEventSource = vi.fn(() => source);
    const api = createProjectApi({baseUrl: 'https://ui.example.test', fetcher: fetcher as any, createEventSource: createEventSource as any});

    await api.regenerateScene('project_1', 'scene_002');
    await api.renderProject('project_1');
    const subscription = api.subscribeToProjectEvents('project_1', vi.fn(), vi.fn());
    subscription.close();

    expect(fetcher).toHaveBeenNthCalledWith(1, 'https://ui.example.test/api/projects/project_1/scenes/scene_002/regenerate', {method: 'POST'});
    expect(fetcher).toHaveBeenNthCalledWith(2, 'https://ui.example.test/api/projects/project_1/render', {method: 'POST'});
    expect(createEventSource).toHaveBeenCalledWith('https://ui.example.test/api/projects/project_1/events');
    expect(source.close).toHaveBeenCalled();
  });

  it('places every request and SSE URL below an injected API mount', async () => {
    const fetcher = vi.fn(async () => ({ok: true, json: async () => ({id: 'project_1'})}));
    const createEventSource = vi.fn(() => ({onmessage: null, onerror: null, close() {}}));
    const api = createProjectApi({
      apiBasePath: '/api/video-workbench',
      fetcher: fetcher as never,
      createEventSource: createEventSource as never
    });
    await api.createProject({} as never);
    api.subscribeToProjectEvents('project_1', () => undefined, () => undefined);
    expect(fetcher).toHaveBeenCalledWith('/api/video-workbench/projects', expect.anything());
    expect(createEventSource).toHaveBeenCalledWith('/api/video-workbench/projects/project_1/events');
  });

  it('rejects malformed JSON responses without making an external call in tests', async () => {
    const fetcher = vi.fn().mockResolvedValue({ok: true, json: async () => ({status: 'RENDERING'})});
    const api = createProjectApi({fetcher: fetcher as any, createEventSource: vi.fn() as any});

    await expect(api.getProject('project_1')).rejects.toThrow('malformed project response');
    expect(fetcher).toHaveBeenCalledWith('/api/projects/project_1', {method: 'GET'});
  });

  it('reports malformed SSE JSON and malformed project shapes to the injected subscriber', () => {
    const source: {onmessage: ((event: {data: string}) => void) | null; onerror: (() => void) | null; close(): void} = {onmessage: null, onerror: null, close() {}};
    const onProject = vi.fn();
    const api = createProjectApi({fetcher: vi.fn() as any, createEventSource: () => source});
    api.subscribeToProjectEvents('project_1', onProject, vi.fn());

    source.onmessage?.({data: '{not-json'});
    source.onmessage?.({data: JSON.stringify({id: 'project_1', title: 'Broken', status: 'RENDERING', scenes: [{id: 'scene_001', order: 1, status: 'READY', script: 'Text', visual: null}], assetVersions: []})});

    expect(onProject).toHaveBeenNthCalledWith(1, undefined);
    expect(onProject).toHaveBeenNthCalledWith(2, undefined);
  });

  it('consumes named project events emitted by the API SSE protocol and removes the listener on close', () => {
    const listeners = new Map<string, (event: {data: string}) => void>();
    const source = {
      onmessage: null as ((event: {data: string}) => void) | null,
      onerror: null as (() => void) | null,
      addEventListener: vi.fn((name: string, listener: (event: {data: string}) => void) => listeners.set(name, listener)),
      removeEventListener: vi.fn((name: string) => listeners.delete(name)), close: vi.fn()
    };
    const onProject = vi.fn();
    const api = createProjectApi({fetcher: vi.fn() as any, createEventSource: () => source});
    const subscription = api.subscribeToProjectEvents('project_1', onProject, vi.fn());
    const frame = 'event: project\ndata: {"id":"project_1","title":"Live","status":"RENDERING","scenes":[],"assetVersions":[]}\n\n';
    const data = frame.split('\n').find((line) => line.startsWith('data: '))!.slice(6);
    listeners.get('project')?.({data});
    subscription.close();

    expect(onProject).toHaveBeenCalledWith(expect.objectContaining({id: 'project_1'}));
    expect(source.removeEventListener).toHaveBeenCalledWith('project', expect.any(Function));
  });
});
