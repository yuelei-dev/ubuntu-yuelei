import {describe, expect, it} from 'vitest';
import {InMemoryProjectRepository, type JobRecord, type ProjectRecord} from './project-service.js';

const at = (milliseconds: number): string => new Date(milliseconds).toISOString();

const project = (): ProjectRecord => ({
  id: 'project_fence', ownerUsername: 'alice', title: 'fence', status: 'QUALITY_CHECK',
  input: {type: 'script', content: 'fence'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'vertical'},
  createdAt: at(0), updatedAt: at(0)
});

const renderJob = (): JobRecord => ({
  id: 'project_fence:project:project.render:hash', projectId: 'project_fence', sceneId: 'project', taskType: 'project.render',
  inputHash: 'hash', status: 'QUEUED', createdAt: at(0)
});

describe('durable render fencing', () => {
  it('renews before expiry, permits takeover only after expiry, and fences the old owner from publishing', async () => {
    let clockMs = 0;
    const repository = new InMemoryProjectRepository(() => new Date(clockMs));
    await repository.createProject(project());
    await repository.reserveJob(renderJob());
    const ownerOne = 'owner-one';
    const ownerTwo = 'owner-two';

    await expect(repository.claimRenderJobLease(renderJob().id, ownerOne, 100)).resolves.toMatchObject({renderLeaseOwner: ownerOne});
    clockMs = 50;
    await expect(repository.renewRenderJobLease(renderJob().id, ownerOne, 100)).resolves.toMatchObject({renderLeaseExpiresAt: at(150)});
    clockMs = 60;
    await expect(repository.renewRenderJobLease(renderJob().id, ownerOne, 65)).resolves.toBeUndefined();
    expect((await repository.findJob(renderJob().id))?.renderLeaseExpiresAt).toBe(at(150));
    clockMs = 101;
    await expect(repository.claimRenderJobLease(renderJob().id, ownerTwo, 100)).resolves.toBeUndefined();
    clockMs = 151;
    await expect(repository.claimRenderJobLease(renderJob().id, ownerTwo, 99)).resolves.toMatchObject({renderLeaseOwner: ownerTwo});

    await expect(repository.commitRenderJobTerminal({
      projectId: 'project_fence', jobId: renderJob().id, owner: ownerOne, status: 'FAILED', reportPath: 'reports/old.json'
    })).resolves.toBeUndefined();
    await expect(repository.commitRenderJobTerminal({
      projectId: 'project_fence', jobId: renderJob().id, owner: ownerTwo, status: 'COMPLETED', reportPath: 'reports/winner.json',
      output: {previewUrl: 'attempts/owner-two/preview.mp4', downloadUrl: 'attempts/owner-two/preview.mp4'}
    })).resolves.toMatchObject({project: {status: 'COMPLETED', qualityReportPath: 'reports/winner.json'}, job: {status: 'COMPLETED'}});
    expect(await repository.findProjectForWorker('project_fence')).toMatchObject({
      status: 'COMPLETED', qualityReportPath: 'reports/winner.json', previewUrl: 'attempts/owner-two/preview.mp4'
    });
  });
});
