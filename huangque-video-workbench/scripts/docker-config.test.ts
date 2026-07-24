import {describe, expect, it} from 'vitest';
import {parseDockerRuntimeConfig} from './docker-config.js';

const common = {
  DATABASE_URL: 'postgresql://huangque:localdev@postgres:5432/huangque', REDIS_HOST: 'redis', REDIS_PASSWORD: 'localdevredis',
  HUANGQUE_AUTH_BASE: 'https://huangque.example/',
  AVATAR_PROVIDER_ENDPOINT: 'https://avatar.vendor.example/generate',
  IMAGE_PROVIDER_ENDPOINT: 'https://image.vendor.example/generate',
  RENDER_PROVIDER_ENDPOINT: 'https://render.vendor.example/generate',
  PROVIDER_TOKEN: 'provider-secret',
  PROVIDER_ALLOWED_MEDIA_ORIGINS: 'https://media.vendor.example,https://cdn.vendor.example'
};

describe('Docker runtime configuration', () => {
  it('rejects a worker without object-storage credentials', () => {
    expect(() => parseDockerRuntimeConfig(common, 'worker')).toThrow('MINIO_ENDPOINT is required');
  });

  it('parses the cross-container service contract', () => {
    expect(parseDockerRuntimeConfig({...common,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque'
    }, 'worker')).toMatchObject({
      redis: {host: 'redis', port: 6379, password: 'localdevredis'},
      minio: {endPoint: 'minio', port: 9000, bucket: 'huangque'}
    });
  });

  it('requires a complete Huangque authentication base for the production API', () => {
    const {HUANGQUE_AUTH_BASE: _authBase, ...withoutAuthBase} = common;
    const environment = {...withoutAuthBase,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque'
    };

    expect(() => parseDockerRuntimeConfig(environment, 'api')).toThrow('HUANGQUE_AUTH_BASE is required');
  });

  it('accepts only a complete HTTPS Huangque authentication base for the production API', () => {
    const environment = {...common,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque'
    };

    expect(() => parseDockerRuntimeConfig({...environment, HUANGQUE_AUTH_BASE: 'http://127.0.0.1:8095'}, 'api'))
      .toThrow('HUANGQUE_AUTH_BASE must be a complete HTTPS URL');
    expect(parseDockerRuntimeConfig({...environment, HUANGQUE_AUTH_BASE: 'https://huangque.example/'}, 'api'))
      .toMatchObject({huangqueAuthBase: 'https://huangque.example/'});
  });

  it('allows the dedicated container auth service only in explicit development mode', () => {
    const environment = {...common,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque',
      HUANGQUE_AUTH_BASE: 'http://auth-dev:8095'
    };

    expect(() => parseDockerRuntimeConfig(environment, 'api')).toThrow('HUANGQUE_AUTH_BASE must be a complete HTTPS URL');
    expect(parseDockerRuntimeConfig(environment, 'api', 'development')).toMatchObject({huangqueAuthBase: 'http://auth-dev:8095/'});
  });

  it.each([
    'http://auth-dev:8095/path', 'http://auth-dev:8095?query=value', 'http://auth-dev:8095#fragment',
    'http://user:password@auth-dev:8095', 'http://auth-dev:8096'
  ])('rejects a non-origin development auth base: %s', (authBase) => {
    const environment = {...common,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque',
      HUANGQUE_AUTH_BASE: authBase
    };

    expect(() => parseDockerRuntimeConfig(environment, 'api', 'development'))
      .toThrow('development HUANGQUE_AUTH_BASE must be http://auth-dev:8095');
  });

  it('does not require an unused authentication base for the worker', () => {
    const {HUANGQUE_AUTH_BASE: _authBase, ...workerEnvironment} = common;

    expect(parseDockerRuntimeConfig({...workerEnvironment,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque'
    }, 'worker')).not.toHaveProperty('huangqueAuthBase');
  });

  it('fails production worker startup when any external provider is not configured', () => {
    const {AVATAR_PROVIDER_ENDPOINT: _endpoint, ...missingAvatar} = common;
    expect(() => parseDockerRuntimeConfig({...missingAvatar,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque'
    }, 'worker')).toThrow('AVATAR_PROVIDER_ENDPOINT is required');
  });

  it.each([
    ['RENDER_PROVIDER_ENDPOINT', 'https://127.0.0.1/render'],
    ['RENDER_PROVIDER_ENDPOINT', 'https://user:secret@render.vendor.example/render'],
    ['RENDER_PROVIDER_ENDPOINT', 'https://render.vendor.example/render#fragment'],
    ['PROVIDER_ALLOWED_MEDIA_ORIGINS', 'https://[::1]']
  ])('rejects unsafe provider configuration %s=%s before startup', (name, value) => {
    expect(() => parseDockerRuntimeConfig({...common,
      MINIO_ENDPOINT: 'minio', MINIO_ACCESS_KEY: 'localdev', MINIO_SECRET_KEY: 'localdevsecret', MINIO_BUCKET: 'huangque',
      [name]: value
    }, 'worker')).toThrow();
  });
});
