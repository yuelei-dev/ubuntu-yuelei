import type {ProjectDetail} from './services/project-service.js';

export type ProjectEventPublisher = {publish(project: ProjectDetail): void};
export type ProjectEventSource = {subscribe(projectId: string, listener: (project: ProjectDetail) => void): () => void};
export type ProjectEventBroker = ProjectEventPublisher & ProjectEventSource;

/** Process-local default; production can inject a durable cross-process broker. */
export class InMemoryProjectEventBroker implements ProjectEventBroker {
  private readonly listeners = new Map<string, Set<(project: ProjectDetail) => void>>();

  publish(project: ProjectDetail): void {
    this.listeners.get(project.id)?.forEach((listener) => listener(project));
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
}

export const sseProjectEvent = (project: ProjectDetail): string => `event: project\ndata: ${JSON.stringify(project)}\n\n`;
export const sseHeartbeat = (): string => ': heartbeat\n\n';

type SseWritable = {write(chunk: string): unknown; once(event: 'close', listener: () => void): unknown};

export const openProjectEventStream = ({
  raw, broker, project, heartbeatMs
}: {
  raw: SseWritable;
  broker: ProjectEventBroker;
  project: ProjectDetail;
  heartbeatMs: number;
}): (() => void) => {
  raw.write(sseProjectEvent(project));
  const unsubscribe = broker.subscribe(project.id, (update) => raw.write(sseProjectEvent(update)));
  const heartbeat = setInterval(() => raw.write(sseHeartbeat()), heartbeatMs);
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    clearInterval(heartbeat);
    unsubscribe();
  };
  raw.once('close', cleanup);
  return cleanup;
};
