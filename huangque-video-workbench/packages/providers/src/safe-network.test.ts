import {describe, expect, it} from 'vitest';
import {assertPublicHttpsUrl, createSsrfSafeFetch, isPublicNetworkAddress, validatePublicDns} from './safe-network.js';

describe('SSRF-safe production networking', () => {
  it.each([
    '0.0.0.0', '10.0.0.1', '127.0.0.1', '169.254.1.1', '172.16.0.1',
    '192.168.0.1', '224.0.0.1', '::', '::1', '::ffff:127.0.0.1',
    'fc00::1', 'fe80::1', 'ff02::1'
  ])('classifies %s as non-public', (address) => {
    expect(isPublicNetworkAddress(address)).toBe(false);
  });

  it.each(['https://[::1]/x', 'https://[::ffff:127.0.0.1]/x', 'https://[fc00::1]/x', 'https://[fe80::1]/x'])(
    'rejects bracketed IPv6 URL %s', (url) => {
      expect(() => assertPublicHttpsUrl(url)).toThrow('non-public');
    });

  it('rejects a hostname when the socket lookup returns any private address', async () => {
    const safeFetch = createSsrfSafeFetch(async () => [{address: '10.0.0.7', family: 4}]);
    await expect(safeFetch('https://provider.example/path')).rejects.toThrow('non-public address');
  });

  it('fails worker startup DNS validation before runtime connections', async () => {
    await expect(validatePublicDns(
      ['https://avatar.vendor.example', 'https://render.vendor.example'],
      async (hostname) => [{address: hostname.startsWith('render') ? '169.254.1.2' : '8.8.8.8', family: 4}]
    )).rejects.toThrow('non-public');
  });
});
