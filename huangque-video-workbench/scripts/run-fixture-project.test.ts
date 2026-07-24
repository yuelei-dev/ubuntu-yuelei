import {describe, expect, it} from 'vitest';
import {waitForTerminalProject} from './run-fixture-project.js';
import {FixtureTimeoutError} from './deadline.js';

const response = (payload: unknown) => ({
  ok: true,
  status: 200,
  json: async () => payload
}) as Response;

describe('waitForTerminalProject', () => {
  it('polls until the project completes', async () => {
    const statuses = ['STORYBOARDING', 'RENDERING', 'COMPLETED'];
    const project = await waitForTerminalProject({
      baseUrl: 'http://fixture.invalid',
      projectId: 'project_123',
      pollIntervalMs: 0,
      timeoutMs: 1_000,
      fetcher: async () => response({id: 'project_123', status: statuses.shift(), previewUrl: '/preview.mp4'})
    });

    expect(project).toMatchObject({status: 'COMPLETED', previewUrl: '/preview.mp4'});
  });

  it('accepts the owner-authenticated output route used by production responses', async () => {
    const project = await waitForTerminalProject({
      baseUrl: 'http://fixture.invalid',
      projectId: 'project_private',
      pollIntervalMs: 0,
      timeoutMs: 1_000,
      fetcher: async () => response({
        id: 'project_private',
        status: 'COMPLETED',
        previewUrl: '/api/projects/project_private/output'
      })
    });

    expect(project.previewUrl).toBe('/api/projects/project_private/output');
  });

  it('reports a non-success terminal status', async () => {
    await expect(waitForTerminalProject({
      baseUrl: 'http://fixture.invalid',
      projectId: 'project_123',
      pollIntervalMs: 0,
      timeoutMs: 1_000,
      fetcher: async () => response({id: 'project_123', status: 'FAILED'})
    })).rejects.toThrow('project project_123 reached FAILED');
  });

  it('bounds a status request even when a fetch implementation never settles', async () => {
    await expect(waitForTerminalProject({
      baseUrl: 'http://fixture.invalid',
      projectId: 'project_stuck',
      timeoutMs: 10,
      fetcher: async () => new Promise<Response>(() => undefined)
    })).rejects.toBeInstanceOf(FixtureTimeoutError);
  });
});
