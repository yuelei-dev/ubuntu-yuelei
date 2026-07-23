import {describe, expect, it} from 'vitest';
import {createProjectApi} from '@huangque/web';
import {createApp} from '../app.js';
import {InMemoryProjectRepository} from '../services/project-service.js';
import {InMemoryQueue} from '../queue.js';

describe('web/API project contract', () => {
  it('returns a project detail that the shared web runtime schema accepts', async () => {
    const app = createApp({repository: new InMemoryProjectRepository(), queue: new InMemoryQueue(), idFactory: () => 'project_001'});
    const api = createProjectApi({
      fetcher: async (url, init) => {
        const response = await app.inject({
          method: init.method,
          url,
          payload: init.body ? JSON.parse(init.body) : undefined
        });
        return {ok: response.statusCode >= 200 && response.statusCode < 300, status: response.statusCode, json: async () => response.json()};
      },
      createEventSource: (() => ({onmessage: null, onerror: null, close() {}})) as never
    });

    const created = await api.createProject({
      input: {type: 'script', content: 'A compatible script'},
      avatar: {avatarId: 'avatar', voiceId: 'voice'},
      output: {templateId: 'vertical_knowledge_v1'}
    });
    const project = await api.getProject(created.id);

    expect(project).toMatchObject({id: 'project_001', status: 'CREATED', scenes: [], assetVersions: []});
    await app.close();
  });
});
