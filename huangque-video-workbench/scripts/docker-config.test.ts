import {describe, expect, it} from 'vitest';
import {parseDockerRuntimeConfig} from './docker-config.js';

const common = {DATABASE_URL: 'postgresql://huangque:localdev@postgres:5432/huangque', REDIS_HOST: 'redis'};

describe('Docker runtime configuration', () => {
  it('rejects a worker without object-storage credentials', () => {
    expect(() => parseDockerRuntimeConfig(common, 'worker')).toThrow('MINIO_ENDPOINT is required');
  });

  it('parses the cross-container service contract', () => {
    expect(parseDockerRuntimeConfig({...common,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret',
      MINIO_BUCKET: 'huangque', MINIO_PUBLIC_ENDPOINT: 'http://localhost:9000/'
    }, 'worker')).toMatchObject({
      redis: {host: 'redis', port: 6379},
      minio: {endPoint: 'minio', port: 9000, bucket: 'huangque', publicEndpoint: 'http://localhost:9000'}
    });
  });
});
