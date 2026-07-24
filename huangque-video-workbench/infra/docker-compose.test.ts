import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {parse} from 'yaml';
import {describe, expect, it} from 'vitest';

describe('production-like Docker composition', () => {
  it('pins PostgreSQL and MinIO state to stable named volumes', async () => {
    const compose = parse(await readFile(resolve('infra', 'docker-compose.yml'), 'utf8')) as {
      services: Record<string, {volumes?: string[]}>;
      volumes?: Record<string, {name?: string}>;
    };

    expect(compose.services.postgres?.volumes).toEqual(['postgres_data:/var/lib/postgresql/data']);
    expect(compose.services.minio?.volumes).toEqual(['minio_data:/data']);
    expect(compose.volumes).toEqual({
      postgres_data: {name: 'huangque-video-workbench-postgres-data'},
      minio_data: {name: 'huangque-video-workbench-minio-data'}
    });
  });

  it('wires migration, API, and worker services to PostgreSQL, Redis, and MinIO', async () => {
    const compose = parse(await readFile(resolve('infra', 'docker-compose.yml'), 'utf8')) as {
      services: Record<string, {command?: string; depends_on?: Record<string, {condition?: string}>; environment?: Record<string, string>}>;
    };

    expect(Object.keys(compose.services)).toEqual(expect.arrayContaining(['postgres', 'redis', 'minio', 'migrate', 'api', 'worker']));
    expect(compose.services.migrate?.command).toContain('db:migrate');
    expect(compose.services.api?.command).toContain('docker:api');
    expect(compose.services.worker?.command).toContain('docker:worker');
    expect(compose.services.api?.environment).toMatchObject({DATABASE_URL: expect.stringContaining('DATABASE_URL:?'), REDIS_HOST: 'redis'});
    expect(compose.services.worker?.environment).toMatchObject({
      DATABASE_URL: expect.stringContaining('DATABASE_URL:?'),
      REDIS_HOST: 'redis',
      MINIO_ENDPOINT: 'minio'
    });
    expect(compose.services.api?.depends_on?.migrate?.condition).toBe('service_completed_successfully');
    expect(compose.services.worker?.depends_on?.minio?.condition).toBe('service_healthy');
  });

  it('keeps stateful services private and requires production secrets', async () => {
    const compose = parse(await readFile(resolve('infra', 'docker-compose.yml'), 'utf8')) as {
      services: Record<string, {ports?: string[]; environment?: Record<string, string>; command?: string}>;
    };

    expect(compose.services.postgres?.ports).toBeUndefined();
    expect(compose.services.redis?.ports).toBeUndefined();
    expect(compose.services.minio?.ports).toBeUndefined();
    expect(compose.services.api?.ports).toEqual(['127.0.0.1:4173:4173']);
    expect(compose.services.postgres?.environment?.POSTGRES_PASSWORD).toContain('POSTGRES_PASSWORD:?');
    expect(compose.services.redis?.command).toContain('--requirepass');
    expect(compose.services.api?.environment?.REDIS_PASSWORD).toContain('REDIS_PASSWORD:?');
    expect(compose.services.minio?.environment).toMatchObject({
      MINIO_ROOT_USER: expect.stringContaining('MINIO_ROOT_USER:?'),
      MINIO_ROOT_PASSWORD: expect.stringContaining('MINIO_ROOT_PASSWORD:?')
    });
    for (const service of ['migrate', 'api', 'worker']) {
      expect(compose.services[service]?.environment?.DATABASE_URL).toContain('DATABASE_URL:?');
      expect(compose.services[service]?.environment?.DATABASE_URL).not.toContain('POSTGRES_PASSWORD');
    }
    expect(compose.services.api?.environment?.HUANGQUE_AUTH_BASE).toContain('HUANGQUE_AUTH_BASE:?');
    expect(compose.services.worker?.environment?.HUANGQUE_AUTH_BASE).toBeUndefined();
  });

  it('keeps backup and rollback instructions aligned with the stable volume contract', async () => {
    const runbook = await readFile(resolve('docs', 'operations', 'production-deployment.md'), 'utf8');

    expect(runbook).toContain('huangque-video-workbench-postgres-data');
    expect(runbook).toContain('huangque-video-workbench-minio-data');
    expect(runbook).toContain('pg_dump');
    expect(runbook).toContain('pg_restore --list');
    expect(runbook).toContain('MinIO data backup');
    expect(runbook).toContain('same stable named volumes');
  });

  it('keeps anonymous object reads out of worker startup and offers an explicit development override', async () => {
    const [worker, development, developmentEnvironment] = await Promise.all([
      readFile(resolve('scripts', 'docker-worker.ts'), 'utf8'),
      readFile(resolve('infra', 'docker-compose.dev.yml'), 'utf8'),
      readFile(resolve('infra', 'docker-compose.dev.env'), 'utf8')
    ]);

    expect(worker).not.toContain('s3:GetObject');
    expect(worker).not.toContain('MINIO_PUBLIC_ENDPOINT');
    expect(development).toContain('5432:5432');
    expect(development).toContain('6379:6379');
    expect(development).toContain('9000:9000');
    expect(developmentEnvironment).toContain('POSTGRES_PASSWORD=localdev');
    expect(developmentEnvironment).toContain('REDIS_PASSWORD=localdevredis');
    expect(developmentEnvironment).toContain('MINIO_ROOT_PASSWORD=localdevsecret');
    expect(developmentEnvironment).toContain('DATABASE_URL=postgresql://huangque:localdev@postgres:5432/huangque');
    expect(development).toContain('auth-dev:');
    expect(development).toContain('docker-dev-auth.ts');
    expect(development).toContain('HUANGQUE_AUTH_BASE: http://auth-dev:8095');
    expect(developmentEnvironment).toContain('HUANGQUE_RUNTIME_MODE=development');
  });
});
