import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {withDeadline} from './deadline.js';

export const FIXTURE_SCRIPT = '???????????????????????????????????????';

type FixtureProject = {
  id: string;
  status: string;
  previewUrl?: string;
  qualityReportPath?: string;
};

type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

const terminalStatuses = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'NEEDS_USER_INPUT']);
const delay = (milliseconds: number, signal: AbortSignal): Promise<void> => new Promise((resolveDelay, rejectDelay) => {
  const timer = setTimeout(resolveDelay, milliseconds);
  signal.addEventListener('abort', () => {
    clearTimeout(timer);
    rejectDelay(signal.reason);
  }, {once: true});
});

const readProject = async (response: Response, action: string): Promise<FixtureProject> => {
  if (!response.ok) throw new Error(`${action} failed with HTTP ${response.status}`);
  const payload = await response.json() as Partial<FixtureProject>;
  if (typeof payload.id !== 'string' || typeof payload.status !== 'string') {
    throw new Error(`${action} returned malformed project data`);
  }
  return payload as FixtureProject;
};

export const waitForTerminalProject = async ({
  baseUrl,
  projectId,
  timeoutMs = 120_000,
  pollIntervalMs = 250,
  fetcher = fetch
}: {
  baseUrl: string;
  projectId: string;
  timeoutMs?: number;
  pollIntervalMs?: number;
  fetcher?: Fetcher;
}): Promise<FixtureProject> => {
  return withDeadline(`project ${projectId} HTTP polling`, timeoutMs, async (signal) => {
    for (;;) {
      const project = await readProject(await fetcher(
        `${baseUrl}/api/projects/${encodeURIComponent(projectId)}`,
        {method: 'GET', signal}
      ), 'project status request');
      if (terminalStatuses.has(project.status)) {
        if (project.status !== 'COMPLETED') throw new Error(`project ${projectId} reached ${project.status}`);
        if (!project.previewUrl?.endsWith('/preview.mp4')) throw new Error(`project ${projectId} completed without a preview MP4 URL`);
        return project;
      }
      await delay(pollIntervalMs, signal);
    }
  });
};

export const runFixtureProject = async ({
  baseUrl,
  timeoutMs = 120_000,
  fetcher = fetch
}: {
  baseUrl: string;
  timeoutMs?: number;
  fetcher?: Fetcher;
}): Promise<FixtureProject> => {
  return withDeadline('fixture project creation and completion', timeoutMs, async (signal) => {
    const startedAt = Date.now();
    const response = await fetcher(`${baseUrl}/api/projects`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({
        input: {type: 'script', content: FIXTURE_SCRIPT},
        avatar: {avatarId: 'mock', voiceId: 'mock'},
        output: {templateId: 'vertical_knowledge_v1'}
      }),
      signal
    });
    const created = await readProject(response, 'project creation');
    const remainingMs = Math.max(1, timeoutMs - (Date.now() - startedAt));
    return waitForTerminalProject({baseUrl, projectId: created.id, timeoutMs: remainingMs, fetcher});
  });
};

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

const main = async (): Promise<void> => {
  const {startLocalComposition} = await import('./start-local-composition.js');
  const composition = await startLocalComposition({port: 0});
  try {
    const project = await runFixtureProject({baseUrl: composition.baseUrl});
    process.stdout.write(`${JSON.stringify({projectId: project.id, status: project.status, previewUrl: project.previewUrl, output: composition.finalOutputPath})}\n`);
  } finally {
    await composition.close();
  }
};

if (isMain) void main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});
