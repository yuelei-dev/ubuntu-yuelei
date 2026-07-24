import {describe, expect, it, vi} from 'vitest';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {PassThrough, Readable} from 'node:stream';
import {ensurePrivateBucket, putObjectWithinDeadline, requestProductionRender, uploadAttemptObjects} from './docker-worker.js';

describe('private MinIO bucket startup', () => {
  it('removes an existing anonymous bucket policy before the worker starts', async () => {
    const calls: string[] = [];
    await ensurePrivateBucket({
      bucketExists: async () => true,
      makeBucket: async () => undefined,
      removeBucketPolicy: async (bucket) => { calls.push(bucket); }
    }, 'huangque');

    expect(calls).toEqual(['huangque']);
  });

  it.each(['NoSuchBucketPolicy', 'NoSuchKey', 'NoSuchBucket'])('accepts %s when no policy can be removed', async (code) => {
    await expect(ensurePrivateBucket({
      bucketExists: async () => true,
      makeBucket: async () => undefined,
      removeBucketPolicy: async () => { throw {code}; }
    }, 'huangque')).resolves.toBeUndefined();
  });

  it('rethrows authentication and network failures while revoking a policy', async () => {
    for (const error of [{code: 'AccessDenied'}, new Error('network unavailable')]) {
      await expect(ensurePrivateBucket({
        bucketExists: async () => true,
        makeBucket: async () => undefined,
        removeBucketPolicy: async () => { throw error; }
      }, 'huangque')).rejects.toBe(error);
    }
  });

  it('bounds a never-settling preview upload before a report upload can begin', async () => {
    const calls: string[] = [];
    await expect(putObjectWithinDeadline({
      putObject: async (_bucket, key) => {
        calls.push(key);
        return await new Promise(() => undefined);
      }
    }, 'huangque', 'projects/p/preview.mp4', Buffer.from('video'), 5, {'Content-Type': 'video/mp4'}, new AbortController().signal, 10)).rejects.toMatchObject({name: 'FixtureTimeoutError'});
    expect(calls).toEqual(['projects/p/preview.mp4']);
  });

  it('bounds a never-settling report upload', async () => {
    const calls: string[] = [];
    await expect(putObjectWithinDeadline({
      putObject: async (_bucket, key) => {
        calls.push(key);
        return await new Promise(() => undefined);
      }
    }, 'huangque', 'projects/p/quality.json', Buffer.from('{}'), 2, {'Content-Type': 'application/json'}, new AbortController().signal, 10)).rejects.toMatchObject({name: 'FixtureTimeoutError'});
    expect(calls).toEqual(['projects/p/quality.json']);
  });

  it('destroys a black-hole source so the underlying upload stops at its deadline', async () => {
    const source = new PassThrough();
    let underlyingStopped = false;
    await expect(putObjectWithinDeadline({
      putObject: async (_bucket, _key, stream) => await new Promise((_resolve, reject) => {
        if (Buffer.isBuffer(stream)) throw new Error('expected stream');
        stream.once('error', (error) => { underlyingStopped = true; reject(error); });
        stream.once('close', () => { underlyingStopped = true; reject(new Error('source closed')); });
      })
    }, 'huangque', 'projects/p/preview.mp4', source, 100, {'Content-Type': 'video/mp4'}, new AbortController().signal, 10))
      .rejects.toMatchObject({name: 'FixtureTimeoutError'});
    expect(source.destroyed).toBe(true);
    expect(underlyingStopped).toBe(true);
  });

  it('removes both private attempt keys when either upload fails', async () => {
    const removed: string[] = [];
    let calls = 0;
    await expect(uploadAttemptObjects({
      client: {
        putObject: async () => {
          if (++calls === 2) throw new Error('report failed');
          return {};
        },
        removeObject: async (_bucket, key) => { removed.push(key); }
      },
      bucket: 'huangque', videoKey: 'attempt/preview.mp4', reportKey: 'attempt/quality.json',
      video: Readable.from(Buffer.from('video')), videoSize: 5, report: Buffer.from('{}'),
      signal: new AbortController().signal, timeoutMs: 100
    })).rejects.toThrow('report failed');
    expect(removed).toEqual(['attempt/preview.mp4', 'attempt/quality.json']);
  });

  it('attempts both deletes and preserves the upload error when cleanup is a black hole', async () => {
    const removed: string[] = [];
    const uploadError = new Error('original upload failure');
    const started = Date.now();
    let caught: Error | undefined;
    try {
      await uploadAttemptObjects({
        client: {
          putObject: async () => { throw uploadError; },
          removeObject: async (_bucket, key) => {
            removed.push(key);
            return await new Promise(() => undefined);
          }
        },
        bucket: 'huangque', videoKey: 'attempt/preview.mp4', reportKey: 'attempt/quality.json',
        video: Readable.from(Buffer.from('video')), videoSize: 5, report: Buffer.from('{}'),
        signal: new AbortController().signal, timeoutMs: 10
      });
    } catch (error) {
      caught = error as Error;
    }
    expect(caught).toBe(uploadError);
    expect((caught as Error & {cleanupErrors?: unknown[]}).cleanupErrors).toHaveLength(2);
    expect(removed).toEqual(['attempt/preview.mp4', 'attempt/quality.json']);
    expect(Date.now() - started).toBeLessThan(200);
  });
});

describe('production render provider', () => {
  it('downloads the bound attempt output instead of reporting a fixture as complete', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'render-provider-'));
    const outputPath = resolve(directory, 'attempt.mp4');
    const input = {projectId: 'p', attemptId: 'owner', templateId: 't', scenes: [{script: 'arbitrary'}]};
    const fetchImpl = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({
          outputUrl: 'https://cdn.vendor.example/attempt-owner.mp4',
          inputHash: body.inputHash,
          provenance: 'generated'
        }));
      }
      return new Response(new Uint8Array([1, 2, 3]), {headers: {'content-length': '3'}});
    }) as unknown as typeof fetch;
    try {
      await requestProductionRender({
        endpoint: 'https://render.vendor.example/jobs', token: 'secret', timeoutMs: 1000,
        allowedMediaOrigins: ['https://cdn.vendor.example'],
        input, outputPath, signal: new AbortController().signal, fetch: fetchImpl
      });
      await expect(readFile(outputPath)).resolves.toEqual(Buffer.from([1, 2, 3]));
      expect(fetchImpl).toHaveBeenCalledTimes(2);
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });

  it('rejects unknown render response fields with strict Zod validation', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'render-provider-schema-'));
    const outputPath = resolve(directory, 'attempt.mp4');
    try {
      await expect(requestProductionRender({
        endpoint: 'https://render.vendor.example/jobs', token: 'secret', timeoutMs: 1000,
        allowedMediaOrigins: ['https://cdn.vendor.example'],
        input: {projectId: 'p'}, outputPath, signal: new AbortController().signal,
        fetch: vi.fn(async (_url, init) => {
          const body = JSON.parse(String(init?.body));
          return new Response(JSON.stringify({
            outputUrl: 'https://cdn.vendor.example/a.mp4', inputHash: body.inputHash,
            provenance: 'generated', unexpected: true
          }));
        }) as unknown as typeof fetch
      })).rejects.toThrow();
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });

  it('stops a falsely-small streamed response at the hard byte cap and deletes the partial file', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'render-provider-cap-'));
    const outputPath = resolve(directory, 'attempt.mp4');
    let call = 0;
    try {
      await expect(requestProductionRender({
        endpoint: 'https://render.vendor.example/jobs', token: 'secret', timeoutMs: 1000,
        allowedMediaOrigins: ['https://cdn.vendor.example'], maxOutputBytes: 2,
        input: {projectId: 'p'}, outputPath, signal: new AbortController().signal,
        fetch: vi.fn(async (_url, init) => {
          if (call++ === 0) {
            const body = JSON.parse(String(init?.body));
            return new Response(JSON.stringify({
              outputUrl: 'https://cdn.vendor.example/a.mp4', inputHash: body.inputHash, provenance: 'generated'
            }));
          }
          return new Response(new Uint8Array([1, 2, 3]), {headers: {'content-length': '1'}});
        }) as unknown as typeof fetch
      })).rejects.toThrow('byte limit');
      await expect(readFile(outputPath)).rejects.toMatchObject({code: 'ENOENT'});
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });

  it('cancels a no-content-length black-hole stream at the provider deadline', async () => {
    const directory = await mkdtemp(resolve(tmpdir(), 'render-provider-deadline-'));
    const outputPath = resolve(directory, 'attempt.mp4');
    let call = 0;
    let cancelled = false;
    try {
      await expect(requestProductionRender({
        endpoint: 'https://render.vendor.example/jobs', token: 'secret', timeoutMs: 20,
        allowedMediaOrigins: ['https://cdn.vendor.example'],
        input: {projectId: 'p'}, outputPath, signal: new AbortController().signal,
        fetch: vi.fn(async (_url, init) => {
          if (call++ === 0) {
            const body = JSON.parse(String(init?.body));
            return new Response(JSON.stringify({
              outputUrl: 'https://cdn.vendor.example/a.mp4', inputHash: body.inputHash, provenance: 'generated'
            }));
          }
          return new Response(new ReadableStream({
            cancel() { cancelled = true; }
          }));
        }) as unknown as typeof fetch
      })).rejects.toThrow();
      expect(cancelled).toBe(true);
      await expect(readFile(outputPath)).rejects.toMatchObject({code: 'ENOENT'});
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });
});
