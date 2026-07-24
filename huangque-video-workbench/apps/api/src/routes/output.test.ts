import {Readable} from 'node:stream';
import {describe, expect, it} from 'vitest';
import {createApp} from '../app.js';
import type {HuangqueIdentity} from '../auth/huangque-auth.js';
import {InMemoryQueue} from '../queue.js';
import {InMemoryProjectRepository} from '../services/project-service.js';

const project = {
  id: 'project_001', ownerUsername: 'alice', title: 'Private output', status: 'COMPLETED' as const,
  input: {type: 'script' as const, content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'},
  previewUrl: 'projects/project_001/preview.mp4', createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString()
};

const createOutputApp = async (identity: HuangqueIdentity) => {
  const repository = new InMemoryProjectRepository();
  await repository.createProject(project);
  const opened: string[] = [];
  const app = createApp({
    repository, queue: new InMemoryQueue(), idFactory: () => 'unused', authenticate: async () => identity,
    objectReader: {open: async (objectKey) => {
      opened.push(objectKey);
      return Readable.from(Buffer.from('private-video'));
    }}
  });
  return {app, opened};
};

describe('authenticated output delivery', () => {
  it('emits the configured deployment API prefix instead of an upstream-internal URL', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(project);
    const app = createApp({
      repository,
      queue: new InMemoryQueue(),
      idFactory: () => 'unused',
      authenticate: async () => ({username: 'alice', role: 'user'}),
      publicApiBasePath: '/api/video-workbench'
    });
    const response = await app.inject({method: 'GET', url: '/api/projects/project_001'});
    expect(response.json()).toMatchObject({previewUrl: '/api/video-workbench/projects/project_001/output'});
    await app.close();
  });
  it('streams a project output only after verifying its Huangque owner', async () => {
    const {app, opened} = await createOutputApp({username: 'alice', role: 'editor'});

    const response = await app.inject({method: 'GET', url: '/api/projects/project_001/output'});

    expect(response.statusCode).toBe(200);
    expect(response.headers['content-type']).toContain('video/mp4');
    expect(response.body).toBe('private-video');
    expect(opened).toEqual(['projects/project_001/preview.mp4']);
    await app.close();
  });

  it('provides the owner-authenticated output route without a public-object URL', async () => {
    const {app} = await createOutputApp({username: 'alice', role: 'editor'});

    const response = await app.inject({method: 'GET', url: '/projects/project_001/output'});

    expect(response.statusCode).toBe(200);
    await app.close();
  });

  it('returns the authenticated output route instead of exposing the persisted object key', async () => {
    const {app} = await createOutputApp({username: 'alice', role: 'editor'});

    const response = await app.inject({method: 'GET', url: '/api/projects/project_001'});

    expect(response.json()).toMatchObject({previewUrl: '/api/projects/project_001/output'});
    expect(response.body).not.toContain('projects/project_001/preview.mp4');
    await app.close();
  });

  it('aliases a download key even when no preview key exists', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject({...project, id: 'download_only', previewUrl: undefined, downloadUrl: 'projects/download_only/final.mp4'});
    const app = createApp({repository, queue: new InMemoryQueue(), idFactory: () => 'unused', authenticate: async () => ({username: 'alice', role: 'editor'})});

    const response = await app.inject({method: 'GET', url: '/api/projects/download_only'});

    expect(response.json()).toMatchObject({downloadUrl: '/api/projects/download_only/output'});
    expect(response.json().previewUrl).toBeUndefined();
    expect(response.body).not.toContain('projects/download_only/final.mp4');
    await app.close();
  });

  it('opens and streams the download key when the project has no preview key', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject({...project, id: 'download_stream', previewUrl: undefined, downloadUrl: 'projects/download_stream/final.mp4'});
    const opened: string[] = [];
    const app = createApp({
      repository, queue: new InMemoryQueue(), idFactory: () => 'unused', authenticate: async () => ({username: 'alice', role: 'editor'}),
      objectReader: {open: async (key) => { opened.push(key); return Readable.from(Buffer.from('download-video')); }}
    });

    const response = await app.inject({method: 'GET', url: '/api/projects/download_stream/output'});

    expect(response.statusCode).toBe(200);
    expect(response.body).toBe('download-video');
    expect(opened).toEqual(['projects/download_stream/final.mp4']);
    await app.close();
  });

  it('returns 404 without reading an output owned by another Huangque user', async () => {
    const {app, opened} = await createOutputApp({username: 'bob', role: 'editor'});

    const response = await app.inject({method: 'GET', url: '/api/projects/project_001/output'});

    expect(response.statusCode).toBe(404);
    expect(response.json()).toEqual({error: 'not_found', resource: 'project'});
    expect(opened).toEqual([]);
    await app.close();
  });
});
