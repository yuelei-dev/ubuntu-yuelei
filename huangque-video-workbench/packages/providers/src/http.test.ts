import {describe, expect, it, vi} from 'vitest';
import {HttpAvatarProvider, HttpImageProvider, providerInputHash} from './http.js';

describe('production HTTP providers', () => {
  it('passes arbitrary avatar and voice identifiers and verifies the bound response', async () => {
    const request = {
      projectId: 'p', sceneId: 's', text: 'hello', avatarId: 'avatar-custom',
      voiceId: 'voice-custom', width: 1080, height: 1920
    };
    const fetchImpl = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body))).toEqual(request);
      expect(init?.headers).toMatchObject({authorization: 'Bearer secret'});
      return new Response(JSON.stringify({
        uri: 'https://media.vendor.example/attempt.mp4', width: 1080, height: 1920,
        provenance: 'generated', inputHash: providerInputHash(request)
      }), {status: 200, headers: {'content-type': 'application/json'}});
    }) as unknown as typeof fetch;
    await expect(new HttpAvatarProvider({
      endpoint: 'https://avatar.vendor.example/v1/generate', token: 'secret', timeoutMs: 1000,
      allowedMediaOrigins: ['https://media.vendor.example'], fetch: fetchImpl
    }).generate(request)).resolves.toMatchObject({provenance: 'generated'});
  });

  it('passes arbitrary prompts and rejects unbound or unsafe responses', async () => {
    const request = {projectId: 'p', sceneId: 's', prompt: 'a unique prompt', width: 1080, height: 1920};
    const provider = new HttpImageProvider({
      endpoint: 'https://image.vendor.example/generate', token: 'secret', timeoutMs: 1000,
      allowedMediaOrigins: ['https://media.vendor.example'],
      fetch: vi.fn(async () => new Response(JSON.stringify({
        uri: 'file:///etc/passwd', width: 1080, height: 1920,
        provenance: 'generated', inputHash: providerInputHash(request)
      }))) as unknown as typeof fetch
    });
    await expect(provider.generate(request)).rejects.toThrow('explicitly allowed');
  });

  it('rejects private endpoints and inputHash mismatches', async () => {
    expect(() => new HttpImageProvider({
      endpoint: 'https://127.0.0.1/generate', token: 'secret', timeoutMs: 1000,
      allowedMediaOrigins: ['https://media.vendor.example']
    })).toThrow('must not target');
    const request = {projectId: 'p', sceneId: 's', prompt: 'x', width: 1080, height: 1920};
    await expect(new HttpImageProvider({
      endpoint: 'https://image.vendor.example/generate', token: 'secret', timeoutMs: 1000,
      allowedMediaOrigins: ['https://cdn.vendor.example'],
      fetch: vi.fn(async () => new Response(JSON.stringify({
        uri: 'https://cdn.vendor.example/a.png', width: 1080, height: 1920,
        provenance: 'generated', inputHash: '0'.repeat(64)
      }))) as unknown as typeof fetch
    }).generate(request)).rejects.toThrow('inputHash');
  });

  it('never follows provider redirects', async () => {
    const fetchImpl = vi.fn(async () => new Response(null, {
      status: 302, headers: {location: 'https://127.0.0.1/private'}
    })) as unknown as typeof fetch;
    const provider = new HttpImageProvider({
      endpoint: 'https://image.vendor.example/generate', token: 'secret', timeoutMs: 1000,
      allowedMediaOrigins: ['https://cdn.vendor.example'], fetch: fetchImpl
    });
    await expect(provider.generate({
      projectId: 'p', sceneId: 's', prompt: 'x', width: 1080, height: 1920
    })).rejects.toThrow('HTTP 302');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
