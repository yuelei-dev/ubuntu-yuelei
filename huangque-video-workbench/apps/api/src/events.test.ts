import {describe, expect, it, vi} from 'vitest';
import {InMemoryProjectEventBroker, openProjectEventStream} from './events.js';

const project = {
  id: 'project_1', ownerUsername: 'alice', title: 'Live project', status: 'RENDERING' as const,
  input: {type: 'script' as const, content: 'Script'}, avatar: {avatarId: 'avatar', voiceId: 'voice'}, output: {templateId: 'template'},
  createdAt: new Date(0).toISOString(), updatedAt: new Date(0).toISOString(), scenes: [], jobs: [], assetVersions: []
};

describe('openProjectEventStream', () => {
  it('writes initial and broker updates, heartbeats, then cleans up on close', () => {
    vi.useFakeTimers();
    const broker = new InMemoryProjectEventBroker();
    const writes: string[] = [];
    const handlers = new Map<string, () => void>();
    const raw = {write: (value: string) => writes.push(value), once: (event: string, callback: () => void) => { handlers.set(event, callback); }};

    openProjectEventStream({raw, broker, project, heartbeatMs: 1_000});
    broker.publish({...project, status: 'QUALITY_CHECK'});
    vi.advanceTimersByTime(1_000);
    handlers.get('close')?.();
    broker.publish({...project, status: 'COMPLETED'});
    vi.advanceTimersByTime(3_000);

    expect(writes).toEqual([
      expect.stringContaining('event: project\ndata: {"id":"project_1"'),
      expect.stringContaining('"status":"QUALITY_CHECK"'),
      ': heartbeat\n\n'
    ]);
    vi.useRealTimers();
  });

  it('sanitizes completed output locations in initial and update SSE events', () => {
    const broker = new InMemoryProjectEventBroker();
    const writes: string[] = [];
    const raw = {write: (value: string) => writes.push(value), once: () => undefined};
    const completed = {
      ...project, status: 'COMPLETED' as const, previewUrl: 'projects/project_1/preview.mp4',
      downloadUrl: 'projects/project_1/final.mp4', qualityReportPath: 's3://huangque/projects/project_1/quality.json'
    };

    openProjectEventStream({raw, broker, project: completed, heartbeatMs: 60_000});
    broker.publish(completed);

    expect(writes).toHaveLength(2);
    for (const event of writes) {
      expect(event).toContain('/api/projects/project_1/output');
      expect(event).not.toContain('projects/project_1/preview.mp4');
      expect(event).not.toContain('projects/project_1/final.mp4');
      expect(event).not.toContain('s3://');
    }
  });
});
