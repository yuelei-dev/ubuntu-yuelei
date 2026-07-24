import {afterEach, describe, expect, it, vi} from 'vitest';
import {AUTH_UPSTREAM_TIMEOUT_MS, HuangqueAuthenticationError, LEGACY_UNOWNED_USERNAME, authenticateHuangque} from './huangque-auth.js';

afterEach(() => vi.unstubAllGlobals());

describe('authenticateHuangque', () => {
  it('forwards only the Huangque session cookie and returns the canonical identity', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({user: {username: 'alice', role: 'editor'}})
    });
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;

    await expect(authenticateHuangque('theme=dark; hq_session=trusted-session; csrf=discard-me', signal))
      .resolves.toEqual({username: 'alice', role: 'editor'});
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:8095/api/auth/me', expect.objectContaining({
      headers: {cookie: 'hq_session=trusted-session'}
    }));
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({signal: expect.any(AbortSignal)});
  });

  it('uses a configured Huangque authentication base URL', async () => {
    const fetch = vi.fn().mockResolvedValue({ok: true, json: async () => ({user: {username: 'alice', role: 'editor'}})});
    vi.stubGlobal('fetch', fetch);

    await authenticateHuangque('hq_session=trusted-session', new AbortController().signal, 'https://huangque.example/auth/');

    expect(fetch).toHaveBeenCalledWith('https://huangque.example/auth/api/auth/me', expect.anything());
  });

  it('rejects an absent or invalid session as unauthorized', async () => {
    await expect(authenticateHuangque(undefined, new AbortController().signal))
      .rejects.toMatchObject({statusCode: 401});

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok: false, status: 401}));
    await expect(authenticateHuangque('hq_session=invalid', new AbortController().signal))
      .rejects.toBeInstanceOf(HuangqueAuthenticationError);
  });

  it('fails closed when Huangque authentication is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('connection refused')));

    await expect(authenticateHuangque('hq_session=trusted-session', new AbortController().signal))
      .rejects.toMatchObject({statusCode: 503});
  });

  it('fails closed when Huangque returns a malformed successful identity', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok: true, json: async () => ({user: {username: 'alice'}})}));

    await expect(authenticateHuangque('hq_session=trusted-session', new AbortController().signal))
      .rejects.toMatchObject({statusCode: 503});
  });

  it('fails closed when the Huangque upstream stalls past the timeout', async () => {
    vi.stubGlobal('fetch', vi.fn((_url: string, init: {signal: AbortSignal}) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    })));

    const startedAt = Date.now();
    await expect(authenticateHuangque('hq_session=trusted-session', new AbortController().signal))
      .rejects.toMatchObject({statusCode: 503});
    expect(Date.now() - startedAt).toBeGreaterThanOrEqual(AUTH_UPSTREAM_TIMEOUT_MS - 100);
  });

  it('fails closed when the client cancels the authentication request', async () => {
    vi.stubGlobal('fetch', vi.fn((_url: string, init: {signal: AbortSignal}) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    })));
    const client = new AbortController();
    const authentication = authenticateHuangque('hq_session=trusted-session', client.signal);
    client.abort();

    await expect(authentication).rejects.toMatchObject({statusCode: 503});
  });

  it('rejects the reserved legacy quarantine username', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({user: {username: LEGACY_UNOWNED_USERNAME, role: 'editor'}})
    }));

    await expect(authenticateHuangque('hq_session=trusted-session', new AbortController().signal))
      .rejects.toMatchObject({statusCode: 401});
  });
});
