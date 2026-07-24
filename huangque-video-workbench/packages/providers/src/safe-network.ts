import {lookup as dnsLookup} from 'node:dns';
import {request as httpsRequest} from 'node:https';
import {isIP} from 'node:net';
import {Readable} from 'node:stream';

export const isPublicNetworkAddress = (raw: string): boolean => {
  const address = raw.replace(/^\[|\]$/g, '').toLowerCase();
  const version = isIP(address);
  if (version === 4) {
    const [a, b] = address.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
      (a === 192 && b === 0) || (a === 192 && b === 2) ||
      (a === 198 && (b === 18 || b === 19 || b === 51)) ||
      (a === 203 && b === 0));
  }
  if (version === 6) {
    const first = Number.parseInt(address.split(':', 1)[0] ?? '', 16);
    return first >= 0x2000 && first <= 0x3fff && !address.startsWith('2001:db8:');
  }
  return false;
};

export const assertPublicHttpsUrl = (raw: string, label = 'URL'): URL => {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.hash) {
    throw new Error(`${label} must be a credential-free HTTPS URL`);
  }
  const hostname = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (isIP(hostname) ? !isPublicNetworkAddress(hostname) :
    hostname === 'localhost' || hostname.endsWith('.localhost') ||
    hostname.endsWith('.local') || hostname.endsWith('.internal')) {
    throw new Error(`${label} must not target a non-public network`);
  }
  return url;
};

export type AddressResolver = (hostname: string) => Promise<Array<{address: string; family: number}>>;

const defaultResolver: AddressResolver = (hostname) => new Promise((resolve, reject) => {
  dnsLookup(hostname, {all: true, verbatim: true}, (error, addresses) => error ? reject(error) : resolve(addresses));
});

export const validatePublicDns = async (
  urls: Array<string | URL>,
  resolveAddresses: AddressResolver = defaultResolver
): Promise<void> => {
  for (const raw of urls) {
    const url = assertPublicHttpsUrl(String(raw));
    const hostname = url.hostname.replace(/^\[|\]$/g, '');
    if (isIP(hostname)) continue;
    const addresses = await resolveAddresses(hostname);
    if (addresses.length === 0 || addresses.some(({address}) => !isPublicNetworkAddress(address))) {
      throw new Error(`DNS resolution for ${hostname} returned a non-public address`);
    }
  }
};

/** HTTPS fetch whose actual socket lookup rejects every non-public answer. */
export const createSsrfSafeFetch = (resolveAddresses: AddressResolver = defaultResolver): typeof fetch =>
  (async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = assertPublicHttpsUrl(input instanceof Request ? input.url : String(input));
    return new Promise<Response>((resolve, reject) => {
      const request = httpsRequest(url, {
        method: init.method,
        headers: init.headers as Record<string, string>,
        signal: init.signal ?? undefined,
        lookup(hostname, _options, callback) {
          void resolveAddresses(hostname).then((addresses) => {
            if (addresses.length === 0 || addresses.some(({address}) => !isPublicNetworkAddress(address))) {
              callback(new Error('DNS resolution returned a non-public address'), '', 4);
              return;
            }
            const selected = addresses[0]!;
            callback(null, selected.address, selected.family);
          }, (error) => callback(error as Error, '', 4));
        }
      }, (incoming) => {
        const headers = new Headers();
        for (const [name, value] of Object.entries(incoming.headers)) {
          if (Array.isArray(value)) value.forEach((item) => headers.append(name, item));
          else if (value !== undefined) headers.set(name, value);
        }
        resolve(new Response(Readable.toWeb(incoming) as ReadableStream, {
          status: incoming.statusCode ?? 500,
          statusText: incoming.statusMessage,
          headers
        }));
      });
      request.once('error', reject);
      if (typeof init.body === 'string' || init.body instanceof Uint8Array) request.write(init.body);
      else if (init.body !== undefined && init.body !== null) {
        request.destroy(new Error('SSRF-safe fetch accepts only string or byte request bodies'));
        return;
      }
      request.end();
    });
  }) as typeof fetch;
