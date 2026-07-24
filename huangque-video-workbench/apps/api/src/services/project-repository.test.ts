import {describe, expect, it} from 'vitest';
import {InMemoryProjectRepository, type ProjectRecord, type SceneRecord} from './project-service.js';

const projectRecord = (status: ProjectRecord['status'] = 'CREATED'): ProjectRecord => ({
  id: 'project_001',
  ownerUsername: 'alice',
  title: 'fixture',
  status,
  input: {type: 'script', content: 'fixture'},
  avatar: {avatarId: 'mock', voiceId: 'mock'},
  output: {templateId: 'vertical_knowledge_v1'},
  createdAt: new Date(0).toISOString(),
  updatedAt: new Date(0).toISOString()
});

const sceneRecord = (status = 'READY'): SceneRecord => ({
  id: 'scene_001',
  projectId: 'project_001',
  order: 1,
  status,
  script: 'fixture',
  visual: {activeGenerationJobId: 'job_001'},
  createdAt: new Date(0).toISOString(),
  updatedAt: new Date(0).toISOString()
});

describe('InMemoryProjectRepository project state claims', () => {
  it('fails closed when an owner-facing operation omits the owner username', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord());
    await repository.createScene(sceneRecord());
    const ownerless = repository as unknown as {
      findProject(id: string, ownerUsername: undefined): Promise<unknown>;
      updateScene(projectId: string, sceneId: string, patch: {script: string}, ownerUsername: undefined): Promise<unknown>;
    };

    await expect(ownerless.findProject('project_001', undefined)).resolves.toBeUndefined();
    await expect(ownerless.updateScene('project_001', 'scene_001', {script: 'ownerless'}, undefined)).resolves.toBeUndefined();
    await expect(repository.findProject('project_001', 'alice')).resolves.toMatchObject({scenes: [{script: 'fixture'}]});
  });

  it('scopes project details and scene updates to the requested owner', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord());
    await repository.createScene(sceneRecord());

    await expect(repository.findProject('project_001', 'bob')).resolves.toBeUndefined();
    await expect(repository.updateScene('project_001', 'scene_001', {script: 'other user'}, 'bob')).resolves.toBeUndefined();
    await expect(repository.findProject('project_001', 'alice')).resolves.toMatchObject({scenes: [{script: 'fixture'}]});
  });

  it('allows exactly one concurrent transition from an expected project status', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord());

    const claims = await Promise.all([
      repository.claimProjectStatus('project_001', 'CREATED', 'STORYBOARDING'),
      repository.claimProjectStatus('project_001', 'CREATED', 'STORYBOARDING')
    ]);

    expect(claims.filter(Boolean)).toHaveLength(1);
    expect((await repository.findProjectForWorker('project_001'))?.status).toBe('STORYBOARDING');
  });

  it.each(['READY', 'FALLBACK_ACCEPTED', 'FAILED'])(
    'atomically reopens a completed project and activates one %s scene for regeneration',
    async (sceneStatus) => {
      const repository = new InMemoryProjectRepository();
      await repository.createProject(projectRecord('COMPLETED'));
      await repository.createScene(sceneRecord(sceneStatus));
      await repository.createAssetVersion({
        id: 'project_001:scene_001:v1', projectId: 'project_001', sceneId: 'scene_001', version: 1,
        uri: 'mock://old', provenance: 'generated', inputHash: 'old-hash', createdAt: new Date(0).toISOString()
      });

      const result = await repository.activateSceneRegeneration(
        'project_001', 'scene_001', 'job_001', 'job_002', 0, 'GENERATING_ASSETS'
      );
      const persisted = await repository.findProjectForWorker('project_001');

      expect(result).toMatchObject({transitioned: true, project: {status: 'GENERATING_ASSETS'}, scene: {
        status: 'PENDING', visual: {activeGenerationJobId: 'job_002'}
      }});
      expect(persisted?.assetVersions).toMatchObject([{id: 'project_001:scene_001:v1', uri: 'mock://old'}]);
    }
  );

  it('does not activate regeneration over a factual-input project pause', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('NEEDS_USER_INPUT'));
    await repository.createScene(sceneRecord('READY'));

    const result = await repository.activateSceneRegeneration(
      'project_001', 'scene_001', 'job_001', 'job_002', 0, 'GENERATING_ASSETS'
    );
    const persisted = await repository.findProjectForWorker('project_001');

    expect(result).toBeUndefined();
    expect(persisted?.status).toBe('NEEDS_USER_INPUT');
    expect(persisted?.scenes[0]).toMatchObject({status: 'READY', visual: {activeGenerationJobId: 'job_001'}});
  });

  it('atomically marks a terminal scene failure and its project partial', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('GENERATING_ASSETS'));
    await repository.createScene(sceneRecord('GENERATING'));

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001');
    const persisted = await repository.findProjectForWorker('project_001');

    expect(result).toMatchObject({transitioned: true, project: {status: 'PARTIALLY_FAILED'}});
    expect(persisted?.status).toBe('PARTIALLY_FAILED');
    expect(persisted?.scenes[0]?.status).toBe('FAILED');
  });

  it('allows the other legal generation status to become partial', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('GENERATING_AVATAR'));
    await repository.createScene(sceneRecord('GENERATING'));

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001');

    expect(result).toMatchObject({transitioned: true, project: {status: 'PARTIALLY_FAILED'}});
    expect((await repository.findProjectForWorker('project_001'))?.scenes[0]?.status).toBe('FAILED');
  });

  it('preserves a factual pause during a concurrent generated-scene terminal failure', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('GENERATING_ASSETS'));
    await repository.createScene(sceneRecord('GENERATING'));

    const [pause, failure] = await Promise.all([
      repository.claimProjectStatus('project_001', 'GENERATING_ASSETS', 'NEEDS_USER_INPUT'),
      repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001')
    ]);
    const persisted = await repository.findProjectForWorker('project_001');

    expect(pause?.status).toBe('NEEDS_USER_INPUT');
    expect(failure).toMatchObject({transitioned: false, project: {status: 'NEEDS_USER_INPUT'}});
    expect(persisted?.status).toBe('NEEDS_USER_INPUT');
    expect(persisted?.scenes[0]?.status).toBe('FAILED');
  });

  it('lets the factual coordinator claim priority when terminal failure wins the first race', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('GENERATING_ASSETS'));
    await repository.createScene(sceneRecord('GENERATING'));

    const [failure, stalePause] = await Promise.all([
      repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001'),
      repository.claimProjectStatus('project_001', 'GENERATING_ASSETS', 'NEEDS_USER_INPUT')
    ]);
    const afterRace = await repository.findProjectForWorker('project_001');
    const pause = await repository.claimProjectStatus('project_001', afterRace!.status, 'NEEDS_USER_INPUT');

    expect(failure).toMatchObject({transitioned: true, project: {status: 'PARTIALLY_FAILED'}});
    expect(stalePause).toBeUndefined();
    expect(pause?.status).toBe('NEEDS_USER_INPUT');
    expect((await repository.findProjectForWorker('project_001'))?.scenes[0]?.status).toBe('FAILED');
  });

  it('marks another failed scene without transitioning an already-partial project', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('PARTIALLY_FAILED'));
    await repository.createScene(sceneRecord('GENERATING'));

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001');

    expect(result).toMatchObject({transitioned: false, project: {status: 'PARTIALLY_FAILED'}});
    expect((await repository.findProjectForWorker('project_001'))?.scenes[0]?.status).toBe('FAILED');
  });

  it.each<ProjectRecord['status']>([
    'CREATED',
    'STORYBOARDING',
    'ALIGNING_TIMELINE',
    'RENDERING',
    'QUALITY_CHECK',
    'COMPLETED',
    'RETRYING',
    'FAILED',
    'CANCELLED'
  ])('refuses terminal scene failure from project status %s', async (status) => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord(status));
    await repository.createScene(sceneRecord());

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001');
    const persisted = await repository.findProjectForWorker('project_001');

    expect(result).toMatchObject({transitioned: false, project: {status}});
    expect(persisted?.status).toBe(status);
    expect(persisted?.scenes[0]?.status).toBe('READY');
  });

  it('does not regress a completed project or its ready scene for a stale failure', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('COMPLETED'));
    await repository.createScene(sceneRecord());

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001');
    const persisted = await repository.findProjectForWorker('project_001');

    expect(result).toMatchObject({transitioned: false, project: {status: 'COMPLETED'}});
    expect(persisted?.status).toBe('COMPLETED');
    expect(persisted?.scenes[0]?.status).toBe('READY');
  });

  it('ignores terminal failure from a superseded generation identity', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('GENERATING_ASSETS'));
    await repository.createScene(sceneRecord('GENERATING'));

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'old-job');
    const persisted = await repository.findProjectForWorker('project_001');

    expect(result).toMatchObject({transitioned: false, project: {status: 'GENERATING_ASSETS'}});
    expect(persisted?.status).toBe('GENERATING_ASSETS');
    expect(persisted?.scenes[0]?.status).toBe('GENERATING');
  });

  it('ignores a late terminal callback after the active generation already completed', async () => {
    const repository = new InMemoryProjectRepository();
    await repository.createProject(projectRecord('GENERATING_ASSETS'));
    await repository.createScene(sceneRecord('READY'));

    const result = await repository.markSceneFailedAndProjectPartiallyFailed('project_001', 'scene_001', 'job_001');
    const persisted = await repository.findProjectForWorker('project_001');

    expect(result).toMatchObject({transitioned: false, project: {status: 'GENERATING_ASSETS'}});
    expect(persisted?.status).toBe('GENERATING_ASSETS');
    expect(persisted?.scenes[0]?.status).toBe('READY');
  });
});
