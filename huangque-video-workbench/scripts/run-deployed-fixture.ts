import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import {mkdtemp, readFile, rm, stat, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {isAbsolute, join} from 'node:path';
import {promisify} from 'node:util';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {runFixtureProject} from './run-fixture-project.js';
import {withDeadline} from './deadline.js';
import {z} from 'zod';

type Credential = {kind: 'cookie' | 'header'; path: string};
export type DeployedAcceptanceConfig = {
  baseUrl: string;
  apiBasePath: '/api/video-workbench';
  credential: Credential;
  payloadPath: string;
};

export const parseDeployedAcceptanceConfig = (environment: NodeJS.ProcessEnv): DeployedAcceptanceConfig => {
  const rawBase = environment.DEPLOYED_WORKBENCH_BASE_URL;
  if (!rawBase) throw new Error('DEPLOYED_WORKBENCH_BASE_URL is required');
  const target = new URL(rawBase);
  if (target.protocol !== 'https:') throw new Error('DEPLOYED_WORKBENCH_BASE_URL must use HTTPS');
  if (target.pathname !== '/' || target.search || target.hash || target.username || target.password) {
    throw new Error('DEPLOYED_WORKBENCH_BASE_URL must be an HTTPS origin without credentials or a path');
  }
  const cookiePath = environment.DEPLOYED_WORKBENCH_COOKIE_FILE;
  const headerPath = environment.DEPLOYED_WORKBENCH_HEADER_FILE;
  if (Boolean(cookiePath) === Boolean(headerPath)) {
    throw new Error('configure exactly one of DEPLOYED_WORKBENCH_COOKIE_FILE or DEPLOYED_WORKBENCH_HEADER_FILE');
  }
  const credential: Credential = cookiePath
    ? {kind: 'cookie', path: cookiePath}
    : {kind: 'header', path: headerPath!};
  if (!isAbsolute(credential.path)) throw new Error('credential file path must be absolute');
  const payloadPath = environment.DEPLOYED_WORKBENCH_PAYLOAD_FILE;
  if (!payloadPath || !isAbsolute(payloadPath)) throw new Error('DEPLOYED_WORKBENCH_PAYLOAD_FILE must be an absolute path');
  return {baseUrl: target.origin, apiBasePath: '/api/video-workbench', credential, payloadPath};
};

type CredentialIo = {
  stat(path: string): Promise<{mode: number}>;
  readFile(path: string): Promise<string>;
};

export const readCredentialHeader = async (
  credential: Credential,
  io: CredentialIo = {
    stat,
    readFile: (path) => readFile(path, 'utf8')
  }
): Promise<{cookie: string}> => {
  const metadata = await io.stat(credential.path);
  if ((metadata.mode & 0o777) !== 0o600) throw new Error('credential file must have mode 0600');
  const value = (await io.readFile(credential.path)).trim();
  if (value.includes('\n') || value.includes('\r')) throw new Error('credential file must contain exactly one line');
  const cookie = credential.kind === 'header'
    ? (/^Cookie:\s*(.+)$/i.exec(value)?.[1] ?? '')
    : value;
  if (!cookie || !/(?:^|;\s*)hq_session=[^;]+/.test(cookie)) {
    throw new Error(credential.kind === 'header' ? 'header file must contain one Cookie header with hq_session' : 'cookie file must contain hq_session');
  }
  return {cookie};
};

const AcceptancePayloadSchema = z.object({
  avatarId: z.string().trim().min(1).max(128),
  voiceId: z.string().trim().min(1).max(128),
  templateId: z.string().trim().min(1).max(128)
}).strict();

export const readAcceptancePayload = async (
  path: string,
  io: CredentialIo = {stat, readFile: (value) => readFile(value, 'utf8')}
): Promise<z.infer<typeof AcceptancePayloadSchema>> => {
  const metadata = await io.stat(path);
  if ((metadata.mode & 0o777) !== 0o600) throw new Error('acceptance payload file must have mode 0600');
  let raw: unknown;
  try {
    raw = JSON.parse(await io.readFile(path));
  } catch {
    throw new Error('acceptance payload file must contain valid JSON');
  }
  return AcceptancePayloadSchema.parse(raw);
};

const execFileAsync = promisify(execFile);

export const runDeployedAcceptance = async (
  config: DeployedAcceptanceConfig,
  environment: NodeJS.ProcessEnv = process.env
): Promise<{projectId: string; sha256: string; probe: unknown}> => {
  const headers = await readCredentialHeader(config.credential);
  const generation = await readAcceptancePayload(config.payloadPath);
  const authenticatedFetch: typeof fetch = (input, init = {}) => {
    const mergedHeaders: Record<string, string> = {};
    new Headers(init.headers).forEach((value, key) => { mergedHeaders[key] = value; });
    return fetch(input, {...init, headers: {...mergedHeaders, ...headers}});
  };
  const project = await runFixtureProject({
    baseUrl: config.baseUrl,
    apiBasePath: config.apiBasePath,
    timeoutMs: 180_000,
    fetcher: authenticatedFetch,
    generation
  });
  if (!project.previewUrl) throw new Error('deployed fixture completed without an authenticated output URL');
  const outputUrl = new URL(project.previewUrl, config.baseUrl);
  if (outputUrl.origin !== config.baseUrl || !outputUrl.pathname.startsWith(`${config.apiBasePath}/projects/`)) {
    throw new Error('deployed fixture returned an output URL outside the configured authenticated API mount');
  }
  const bytes = await withDeadline('authenticated deployed output download', 60_000, async (signal) => {
    const response = await authenticatedFetch(outputUrl, {signal});
    if (!response.ok) throw new Error(`authenticated output download failed with HTTP ${response.status}`);
    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.toLowerCase().includes('video/mp4')) throw new Error('authenticated output is not video/mp4');
    return Buffer.from(await response.arrayBuffer());
  });
  if (bytes.length < 12 || bytes.subarray(4, 8).toString('ascii') !== 'ftyp') throw new Error('downloaded output is not an MP4 file');
  const directory = await mkdtemp(join(tmpdir(), 'huangque-deployed-acceptance-'));
  const videoPath = join(directory, 'output.mp4');
  try {
    await writeFile(videoPath, bytes, {mode: 0o600});
    const {stdout} = await execFileAsync(environment.FFPROBE_PATH ?? 'ffprobe', [
      '-v', 'error',
      '-show_entries', 'stream=codec_name,width,height',
      '-show_entries', 'format=duration',
      '-of', 'json',
      videoPath
    ], {timeout: 30_000, windowsHide: true});
    const probe = JSON.parse(stdout) as {streams?: Array<{codec_name?: string; width?: number; height?: number}>; format?: {duration?: string}};
    const codecs = new Set(probe.streams?.map((stream) => stream.codec_name));
    if (!codecs.has('h264') || !codecs.has('aac')) throw new Error('deployed output must contain H.264 video and AAC audio');
    if (!probe.streams?.some((stream) => stream.width === 1080 && stream.height === 1920)) {
      throw new Error('deployed output must be 1080x1920');
    }
    const duration = Number(probe.format?.duration);
    if (!Number.isFinite(duration) || duration <= 0) throw new Error('deployed output duration is invalid');
    return {projectId: project.id, sha256: createHash('sha256').update(bytes).digest('hex'), probe};
  } finally {
    await rm(directory, {recursive: true, force: true});
  }
};

const isMain = process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isMain) void runDeployedAcceptance(parseDeployedAcceptanceConfig(process.env))
  .then(({projectId, sha256, probe}) => {
    process.stdout.write(`${JSON.stringify({projectId, sha256, probe})}\n`);
  })
  .catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'deployed acceptance failed'}\n`);
    process.exitCode = 1;
  });
