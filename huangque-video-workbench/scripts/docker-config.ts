export type DockerRuntimeConfig = {
  databaseUrl: string;
  redis: {host: string; port: number};
  port: number;
  minio?: {endPoint: string; port: number; useSSL: boolean; accessKey: string; secretKey: string; bucket: string; publicEndpoint: string};
};

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

export const parseDockerRuntimeConfig = (environment: NodeJS.ProcessEnv, role: 'api' | 'worker'): DockerRuntimeConfig => {
  const base: DockerRuntimeConfig = {
    databaseUrl: required(environment, 'DATABASE_URL'),
    redis: {host: required(environment, 'REDIS_HOST'), port: port(environment, 'REDIS_PORT', 6379)},
    port: port(environment, 'PORT', 4173)
  };
  if (role === 'api') return base;
  return {...base, minio: {
    endPoint: required(environment, 'MINIO_ENDPOINT'),
    port: port(environment, 'MINIO_PORT', 9000),
    useSSL: environment.MINIO_USE_SSL === 'true',
    accessKey: required(environment, 'MINIO_ACCESS_KEY'),
    secretKey: required(environment, 'MINIO_SECRET_KEY'),
    bucket: required(environment, 'MINIO_BUCKET'),
    publicEndpoint: required(environment, 'MINIO_PUBLIC_ENDPOINT').replace(/\/$/u, '')
  }};
};
