import Redis from 'ioredis';
import type {ProjectEventBroker, ProjectEventPublisher} from '@huangque/api';

type ProjectDetail = Parameters<ProjectEventPublisher['publish']>[0];
const channel = 'huangque:project-events';

export class RedisProjectEventBroker implements ProjectEventBroker {
  private readonly publisher: Redis;
  private readonly subscriber: Redis;
  private readonly listeners = new Map<string, Set<(project: ProjectDetail) => void>>();

  constructor(connection: {host: string; port: number}) {
    this.publisher = new Redis({...connection, maxRetriesPerRequest: null});
    this.subscriber = new Redis({...connection, maxRetriesPerRequest: null});
    this.subscriber.on('message', (_channel, payload) => {
      try {
        const project = JSON.parse(payload) as Partial<ProjectDetail>;
        if (typeof project.id !== 'string') return;
        this.listeners.get(project.id)?.forEach((listener) => listener(project as ProjectDetail));
      } catch {
        // Ignore malformed messages from outside the application channel contract.
      }
    });
  }

  async ready(): Promise<void> {
    await Promise.all([this.publisher.ping(), this.subscriber.subscribe(channel)]);
  }

  publish(project: ProjectDetail): void {
    void this.publisher.publish(channel, JSON.stringify(project)).catch((error) => {
      process.stderr.write(`Redis project event publish failed: ${error instanceof Error ? error.message : String(error)}\n`);
    });
  }

  subscribe(projectId: string, listener: (project: ProjectDetail) => void): () => void {
    const listeners = this.listeners.get(projectId) ?? new Set();
    listeners.add(listener);
    this.listeners.set(projectId, listeners);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) this.listeners.delete(projectId);
    };
  }

  async close(): Promise<void> {
    await Promise.allSettled([this.publisher.quit(), this.subscriber.quit()]);
  }
}
