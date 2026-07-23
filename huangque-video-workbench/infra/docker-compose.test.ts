import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {parse} from 'yaml';
import {describe, expect, it} from 'vitest';

describe('production-like Docker composition', () => {
  it('wires migration, API, and worker services to PostgreSQL, Redis, and MinIO', async () => {
    const compose = parse(await readFile(resolve('infra', 'docker-compose.yml'), 'utf8')) as {
      services: Record<string, {command?: string; depends_on?: Record<string, {condition?: string}>; environment?: Record<string, string>}>;
    };

    expect(Object.keys(compose.services)).toEqual(expect.arrayContaining(['postgres', 'redis', 'minio', 'migrate', 'api', 'worker']));
    expect(compose.services.migrate?.command).toContain('db:migrate');
    expect(compose.services.api?.command).toContain('docker:api');
    expect(compose.services.worker?.command).toContain('docker:worker');
    expect(compose.services.api?.environment).toMatchObject({DATABASE_URL: expect.stringContaining('postgres'), REDIS_HOST: 'redis'});
    expect(compose.services.worker?.environment).toMatchObject({
      DATABASE_URL: expect.stringContaining('postgres'),
      REDIS_HOST: 'redis',
      MINIO_ENDPOINT: 'minio'
    });
    expect(compose.services.api?.depends_on?.migrate?.condition).toBe('service_completed_successfully');
    expect(compose.services.worker?.depends_on?.minio?.condition).toBe('service_healthy');
  });
});
