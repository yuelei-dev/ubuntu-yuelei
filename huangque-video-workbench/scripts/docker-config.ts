import {assertPublicHttpsUrl} from '@huangque/providers';

type DockerRuntimeBase = {
  databaseUrl: string;
  redis: {host: string; port: number; password: string};
  port: number;
  minio: {endPoint: string; port: number; useSSL: boolean; accessKey: string; secretKey: string; bucket: string};
};
export type DockerRuntimeConfig = DockerRuntimeBase & {huangqueAuthBase: string};
export type DockerWorkerRuntimeConfig = DockerRuntimeBase & {
  providers: {
    avatarEndpoint: string;
    imageEndpoint: string;
    renderEndpoint: string;
    token: string;
    timeoutMs: number;
    allowedMediaOrigins: string[];
  };
};
export type DockerRuntimeMode = 'production' | 'development';

const required = (environment: NodeJS.ProcessEnv, name: string): string => {
  const value = environment[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
};

const port = (environment: NodeJS.ProcessEnv, name: string, fallback: number): number => {
  const parsed = Number(environment[name] ?? fallback);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65_535) throw new Error(`${name} must be an integer from 1 to 65535`);
  return parsed;
};

export function parseDockerRuntimeConfig(environment: NodeJS.ProcessEnv, role: 'api', mode?: DockerRuntimeMode): DockerRuntimeConfig;
export function parseDockerRuntimeConfig(environment: NodeJS.ProcessEnv, role: 'worker', mode?: DockerRuntimeMode): DockerWorkerRuntimeConfig;
export function parseDockerRuntimeConfig(
  environment: NodeJS.ProcessEnv, role: 'api' | 'worker', mode: DockerRuntimeMode = 'production'
): DockerRuntimeConfig | DockerWorkerRuntimeConfig {
  const base: Omit<DockerRuntimeBase, 'minio'> = {
    databaseUrl: required(environment, 'DATABASE_URL'),
    redis: {host: required(environment, 'REDIS_HOST'), port: port(environment, 'REDIS_PORT', 6379), password: required(environment, 'REDIS_PASSWORD')},
    port: port(environment, 'PORT', 4173)
  };
  const runtimeBase: DockerRuntimeBase = {...base, minio: {
    endPoint: required(environment, 'MINIO_ENDPOINT'),
    port: port(environment, 'MINIO_PORT', 9000),
    useSSL: environment.MINIO_USE_SSL === 'true',
    accessKey: required(environment, 'MINIO_ACCESS_KEY'),
    secretKey: required(environment, 'MINIO_SECRET_KEY'),
    bucket: required(environment, 'MINIO_BUCKET')
  }};
  if (role === 'worker') {
    const providerTimeoutMs = Number(environment.PROVIDER_TIMEOUT_MS ?? 120_000);
    if (!Number.isInteger(providerTimeoutMs) || providerTimeoutMs < 1 || providerTimeoutMs > 300_000) {
      throw new Error('PROVIDER_TIMEOUT_MS must be an integer from 1 to 300000');
    }
    const allowedMediaOrigins = required(environment, 'PROVIDER_ALLOWED_MEDIA_ORIGINS')
      .split(',').map((value) => value.trim()).filter(Boolean);
    if (allowedMediaOrigins.length === 0) throw new Error('PROVIDER_ALLOWED_MEDIA_ORIGINS is required');
    const avatarEndpoint = assertPublicHttpsUrl(required(environment, 'AVATAR_PROVIDER_ENDPOINT'), 'AVATAR_PROVIDER_ENDPOINT').toString();
    const imageEndpoint = assertPublicHttpsUrl(required(environment, 'IMAGE_PROVIDER_ENDPOINT'), 'IMAGE_PROVIDER_ENDPOINT').toString();
    const renderEndpoint = assertPublicHttpsUrl(required(environment, 'RENDER_PROVIDER_ENDPOINT'), 'RENDER_PROVIDER_ENDPOINT').toString();
    allowedMediaOrigins.forEach((origin) => assertPublicHttpsUrl(origin, 'PROVIDER_ALLOWED_MEDIA_ORIGINS'));
    return {...runtimeBase, providers: {
      avatarEndpoint,
      imageEndpoint,
      renderEndpoint,
      token: required(environment, 'PROVIDER_TOKEN'),
      timeoutMs: providerTimeoutMs,
      allowedMediaOrigins
    }};
  }

  const huangqueAuthBase = required(environment, 'HUANGQUE_AUTH_BASE');
  let authUrl: URL;
  try {
    authUrl = new URL(huangqueAuthBase);
  } catch {
    throw new Error('HUANGQUE_AUTH_BASE must be a complete HTTPS URL');
  }
  const validProductionBase = authUrl.protocol === 'https:';
  const validDevelopmentBase = authUrl.origin === 'http://auth-dev:8095' && authUrl.pathname === '/' &&
    authUrl.search === '' && authUrl.hash === '' && authUrl.username === '' && authUrl.password === '';
  if ((mode === 'production' && !validProductionBase) || (mode === 'development' && !validDevelopmentBase)) {
    throw new Error(mode === 'production'
      ? 'HUANGQUE_AUTH_BASE must be a complete HTTPS URL'
      : 'development HUANGQUE_AUTH_BASE must be http://auth-dev:8095');
  }
  return {...runtimeBase, huangqueAuthBase: authUrl.toString()};
}
