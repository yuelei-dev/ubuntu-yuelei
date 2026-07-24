import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {parse} from 'yaml';
import {describe, expect, it} from 'vitest';

type Step = {
  uses?: string;
  with?: Record<string, string | number>;
  run?: string;
  env?: Record<string, string>;
  if?: string | boolean;
  'continue-on-error'?: boolean;
};

type Job = {
  permissions?: unknown;
  if?: string | boolean;
  'continue-on-error'?: boolean;
  services?: Record<string, {
    image?: string;
    env?: Record<string, string>;
    ports?: Array<string | number>;
    options?: string;
  }>;
  env?: Record<string, string>;
  steps?: Step[];
};

type Workflow = {
  permissions?: Record<string, string>;
  on?: {
    pull_request?: {paths?: string[]};
    push?: {paths?: string[]};
  };
  defaults?: {run?: {'working-directory'?: string}};
  jobs?: Record<string, Job>;
};

const requiredPaths = [
  'huangque-video-workbench/**',
  '.github/workflows/video-workbench.yml'
];

const requiredCommands = [
  'npm ci',
  'npm run typecheck',
  'npm run build',
  'npm test',
  'npm run test:postgres-integration',
  'npm run db:verify',
  'docker compose -f infra/docker-compose.yml config',
  'npm audit --omit=dev --audit-level=high'
];

const normalizeRun = (run: string | undefined) => run?.trim().replace(/\s+/g, ' ');

function validateWorkflow(workflow: Workflow): string[] {
  const errors: string[] = [];
  const check = (condition: unknown, message: string) => {
    if (!condition) errors.push(message);
  };

  check(workflow.permissions?.contents === 'read', 'top-level contents permission must be read');
  check(Object.keys(workflow.permissions ?? {}).length === 1, 'top-level permissions must be least privilege');

  for (const event of ['pull_request', 'push'] as const) {
    const paths = workflow.on?.[event]?.paths ?? [];
    for (const path of requiredPaths) {
      check(paths.includes(path), `${event}.paths must include ${path}`);
    }
  }

  const jobEntries = Object.entries(workflow.jobs ?? {});
  const releaseEntries = jobEntries.filter(([name]) => name === 'release-gates');
  check(releaseEntries.length === 1, 'exactly one release-gates job is required');
  const release = releaseEntries[0]?.[1];
  if (!release) return errors;

  check(release.permissions === undefined, 'release-gates must not expand permissions');
  check(release['continue-on-error'] === undefined, 'release-gates must not allow failure');
  check(release.if === undefined, 'release-gates must not be conditional');
  check(workflow.defaults?.run?.['working-directory'] === 'huangque-video-workbench', 'nested working directory is required');

  const postgres = release.services?.postgres;
  check(postgres?.image === 'postgres:17-alpine', 'PostgreSQL 17 alpine service is required');
  check(postgres?.env?.POSTGRES_USER === 'huangque', 'PostgreSQL service user must match Compose');
  check(postgres?.env?.POSTGRES_PASSWORD === 'ci-postgres-password', 'PostgreSQL service password must be CI-only');
  check(postgres?.env?.POSTGRES_DB === 'huangque', 'PostgreSQL service database must match Compose');
  check(postgres?.options?.includes('pg_isready -U huangque -d huangque'), 'PostgreSQL health check must match its identity');
  check(
    release.env?.TEST_POSTGRES_DATABASE_URL ===
      'postgresql://huangque:ci-postgres-password@127.0.0.1:5432/huangque',
    'mandatory PostgreSQL integration URL must target the service'
  );

  const steps = release.steps ?? [];
  for (const [index, step] of steps.entries()) {
    check(step['continue-on-error'] === undefined, `release-gates step ${index} must not allow failure`);
    check(step.if === undefined, `release-gates step ${index} must not be conditional`);
  }
  const setupNodes = steps.filter((step) => step.uses?.startsWith('actions/setup-node@'));
  check(setupNodes.length === 1, 'exactly one setup-node step is required');
  check(String(setupNodes[0]?.with?.['node-version']) === '22', 'Node 22 is required');

  for (const command of requiredCommands) {
    const matches = steps.filter((step) => normalizeRun(step.run) === command);
    check(matches.length === 1, `exact required command missing or duplicated: ${command}`);
    const step = matches[0];
    check(step?.['continue-on-error'] === undefined, `${command} must not allow failure`);
    check(step?.if === undefined, `${command} must not be conditional`);
  }

  const composeStep = steps.find(
    (step) => normalizeRun(step.run) === 'docker compose -f infra/docker-compose.yml config'
  );
  check(composeStep?.env?.POSTGRES_PASSWORD === 'ci-postgres-password', 'Compose PostgreSQL password must be CI-only');
  check(composeStep?.env?.REDIS_PASSWORD === 'ci-redis-password', 'Compose Redis password must be CI-only');
  check(composeStep?.env?.MINIO_ROOT_USER === 'ci-minio-user', 'Compose MinIO user must be CI-only');
  check(composeStep?.env?.MINIO_ROOT_PASSWORD === 'ci-minio-password', 'Compose MinIO password must be CI-only');
  check(
    composeStep?.env?.DATABASE_URL ===
      'postgresql://huangque:ci-postgres-password@postgres:5432/huangque',
    'Compose DATABASE_URL must match its PostgreSQL service identity'
  );
  check(composeStep?.env?.HUANGQUE_AUTH_BASE === 'https://auth.invalid', 'Compose auth base must be CI-only HTTPS');

  return errors;
}

async function loadWorkflow(): Promise<Workflow> {
  return parse(await readFile(resolve('integration', 'video-workbench.yml'), 'utf8')) as Workflow;
}

describe('parent repository CI workflow contract', () => {
  it('enforces the complete structured release contract', async () => {
    expect(validateWorkflow(await loadWorkflow())).toEqual([]);
  });

  it('rejects mutation attempts that could silently weaken mandatory gates', async () => {
    const original = await loadWorkflow();
    const mutate = (change: (workflow: Workflow) => void) => {
      const clone = structuredClone(original);
      change(clone);
      return validateWorkflow(clone);
    };

    expect(mutate((workflow) => {
      workflow.jobs!['release-gates'].steps!.find((step) => step.run === 'npm test')!['continue-on-error'] = true;
    })).toContain('npm test must not allow failure');

    expect(mutate((workflow) => {
      workflow.jobs!.decoy = {
        steps: [{run: 'npm run test:postgres-integration'}]
      };
      workflow.jobs!['release-gates'].steps = workflow.jobs!['release-gates'].steps!
        .filter((step) => step.run !== 'npm run test:postgres-integration');
    })).toContain('exact required command missing or duplicated: npm run test:postgres-integration');

    expect(mutate((workflow) => {
      workflow.on!.pull_request!.paths = ['huangque-video-workbench/**'];
    })).toContain('pull_request.paths must include .github/workflows/video-workbench.yml');

    expect(mutate((workflow) => {
      workflow.jobs!['release-gates'].steps!.find((step) => step.run === 'npm run db:verify')!.if = 'false';
    })).toContain('npm run db:verify must not be conditional');

    expect(mutate((workflow) => {
      workflow.jobs!['release-gates'].if = 'false';
    })).toContain('release-gates must not be conditional');

    expect(mutate((workflow) => {
      workflow.jobs!['release-gates'].permissions = {contents: 'write'};
    })).toContain('release-gates must not expand permissions');
  });
});
