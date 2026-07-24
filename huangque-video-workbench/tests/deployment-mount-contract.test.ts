import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {describe, expect, it, vi} from 'vitest';
import {createProjectApi} from '../apps/web/src/api/client.js';
import {projectForClient} from '../apps/api/src/routes/output.js';
import {browserShell} from '../scripts/docker-api.js';
import type {ProjectDetail} from '../apps/api/src/services/project-service.js';

describe('deployed mount integration contract', () => {
  it('keeps HTML assets, browser API/SSE, output aliases, and Nginx rewrites coherent', async () => {
    const shell = browserShell({uiBasePath: '/video-workbench', apiBasePath: '/api/video-workbench'});
    expect(shell).toContain('src="/video-workbench/workbench-client.js"');
    expect(shell).toContain('content="/api/video-workbench"');

    const fetcher = vi.fn(async () => ({ok: true, json: async () => ({id: 'project_1'})}));
    const eventSource = vi.fn(() => ({onmessage: null, onerror: null, close() {}}));
    const api = createProjectApi({
      apiBasePath: '/api/video-workbench',
      fetcher: fetcher as never,
      createEventSource: eventSource as never
    });
    await api.createProject({} as never);
    api.subscribeToProjectEvents('project_1', () => undefined, () => undefined);
    expect(fetcher).toHaveBeenCalledWith('/api/video-workbench/projects', expect.anything());
    expect(eventSource).toHaveBeenCalledWith('/api/video-workbench/projects/project_1/events');

    const clientProject = projectForClient({
      id: 'project_1',
      ownerUsername: 'owner',
      title: 'fixture',
      status: 'COMPLETED',
      input: {type: 'script', content: 'fixture'},
      avatar: {avatarId: 'avatar', voiceId: 'voice'},
      output: {templateId: 'template'},
      previewUrl: 'private/object.mp4',
      createdAt: new Date(0).toISOString(),
      updatedAt: new Date(0).toISOString(),
      scenes: [],
      jobs: [],
      assetVersions: []
    } satisfies ProjectDetail, '/api/video-workbench');
    expect(clientProject.previewUrl).toBe('/api/video-workbench/projects/project_1/output');

    const nginx = (await readFile(resolve('infra', 'nginx-video-workbench.conf'), 'utf8')).replace(/#.*$/gm, '');
    expect(nginx).toMatch(/location \/video-workbench\/\s*\{[^}]*proxy_pass http:\/\/127\.0\.0\.1:4173\/;/s);
    expect(nginx).toMatch(/location \/api\/video-workbench\/\s*\{[^}]*proxy_pass http:\/\/127\.0\.0\.1:4173\/api\/;/s);
    expect(await readFile(resolve('scripts', 'docker-api.ts'), 'utf8')).toContain("production.app.get('/api/healthz'");
  });
});
