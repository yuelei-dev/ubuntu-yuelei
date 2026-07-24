import {createHash} from 'node:crypto';
import {z} from 'zod';
import type {AvatarProvider, AvatarRequest, GeneratedAsset, ImageProvider, ImageRequest} from './types.js';
import {assertPublicHttpsUrl, createSsrfSafeFetch} from './safe-network.js';

const assetSchema = z.object({
  uri: z.string().url(),
  durationMs: z.number().positive().optional(),
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  provenance: z.enum(['uploaded', 'enterprise', 'licensed', 'generated']),
  inputHash: z.string().regex(/^[a-f0-9]{64}$/)
}).strict();

export const providerInputHash = (value: unknown): string =>
  createHash('sha256').update(JSON.stringify(value)).digest('hex');

export type HttpProviderConfig = {
  endpoint: string;
  token: string;
  timeoutMs: number;
  allowedMediaOrigins: string[];
  fetch?: typeof fetch;
};

class HttpGenerationProvider {
  private readonly endpoint: URL;
  private readonly fetchImpl: typeof fetch;
  private readonly allowedMediaOrigins: Set<string>;

  constructor(private readonly config: HttpProviderConfig) {
    this.endpoint = assertPublicHttpsUrl(config.endpoint, 'production provider endpoint');
    if (!config.token.trim()) throw new Error('production provider token is required');
    if (!Number.isInteger(config.timeoutMs) || config.timeoutMs < 1 || config.timeoutMs > 300_000) {
      throw new Error('production provider timeout must be between 1 and 300000ms');
    }
    this.fetchImpl = config.fetch ?? createSsrfSafeFetch();
    this.allowedMediaOrigins = new Set(config.allowedMediaOrigins.map((value) => assertPublicHttpsUrl(value, 'allowed media origin').origin));
    if (this.allowedMediaOrigins.size === 0) throw new Error('at least one allowed media origin is required');
  }

  async generate(input: AvatarRequest | ImageRequest): Promise<GeneratedAsset> {
    const body = {...input, signal: undefined};
    const inputHash = providerInputHash(body);
    const timeout = AbortSignal.timeout(this.config.timeoutMs);
    const signal = input.signal ? AbortSignal.any([input.signal, timeout]) : timeout;
    const response = await this.fetchImpl(this.endpoint, {
      method: 'POST',
      headers: {'authorization': `Bearer ${this.config.token}`, 'content-type': 'application/json'},
      body: JSON.stringify(body),
      redirect: 'error',
      signal
    });
    if (!response.ok) throw new Error(`provider request failed with HTTP ${response.status}`);
    const asset = assetSchema.parse(await response.json());
    if (asset.inputHash !== inputHash) throw new Error('provider response inputHash does not match request');
    const uri = new URL(asset.uri);
    if (uri.protocol !== 'https:' || uri.username || uri.password || !this.allowedMediaOrigins.has(uri.origin)) {
      throw new Error('provider response URI must use an explicitly allowed credential-free HTTPS origin');
    }
    if (asset.width !== input.width || asset.height !== input.height) {
      throw new Error('provider response dimensions do not match request');
    }
    return asset;
  }
}

export class HttpAvatarProvider extends HttpGenerationProvider implements AvatarProvider {}
export class HttpImageProvider extends HttpGenerationProvider implements ImageProvider {}
