import {describe, expect, it, vi} from 'vitest';
import {InMemoryProjectEventBroker, openProjectEventStream} from './events.js';

const project = {
  id: 'project_1', title: 'Live project', status: 'RENDERING' as const,
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
});
