import {describe, expect, it} from 'vitest';
import {parseDeployedAcceptanceConfig, readAcceptancePayload, readCredentialHeader} from './run-deployed-fixture.js';

describe('deployed fixture acceptance configuration', () => {
  it('fails closed without an HTTPS target and credential file', () => {
    expect(() => parseDeployedAcceptanceConfig({})).toThrow('DEPLOYED_WORKBENCH_BASE_URL');
    expect(() => parseDeployedAcceptanceConfig({
      DEPLOYED_WORKBENCH_BASE_URL: 'http://example.test',
      DEPLOYED_WORKBENCH_COOKIE_FILE: '/secret/cookie'
    })).toThrow('HTTPS');
    expect(() => parseDeployedAcceptanceConfig({
      DEPLOYED_WORKBENCH_BASE_URL: 'https://example.test',
      DEPLOYED_WORKBENCH_COOKIE_FILE: '/secret/cookie',
      DEPLOYED_WORKBENCH_HEADER_FILE: '/secret/header'
    })).toThrow('exactly one');
  });

  it('accepts one protected credential file without storing its value in config', () => {
    expect(parseDeployedAcceptanceConfig({
      DEPLOYED_WORKBENCH_BASE_URL: 'https://example.test/',
      DEPLOYED_WORKBENCH_COOKIE_FILE: '/secret/cookie',
      DEPLOYED_WORKBENCH_PAYLOAD_FILE: '/secret/payload.json'
    })).toEqual({
      baseUrl: 'https://example.test',
      apiBasePath: '/api/video-workbench',
      credential: {kind: 'cookie', path: '/secret/cookie'},
      payloadPath: '/secret/payload.json'
    });
  });

  it('requires a protected, bounded, non-mock production payload', async () => {
    expect(() => parseDeployedAcceptanceConfig({
      DEPLOYED_WORKBENCH_BASE_URL: 'https://example.test/',
      DEPLOYED_WORKBENCH_COOKIE_FILE: '/secret/cookie'
    })).toThrow('PAYLOAD_FILE');
    await expect(readAcceptancePayload('/secret/payload', {
      stat: async () => ({mode: 0o100640}),
      readFile: async () => '{"avatarId":"real","voiceId":"real","templateId":"real"}'
    })).rejects.toThrow('0600');
    await expect(readAcceptancePayload('/secret/payload', {
      stat: async () => ({mode: 0o100600}),
      readFile: async () => JSON.stringify({avatarId: '', voiceId: 'v', templateId: 't'})
    })).rejects.toThrow();
    await expect(readAcceptancePayload('/secret/payload', {
      stat: async () => ({mode: 0o100600}),
      readFile: async () => JSON.stringify({avatarId: 'a'.repeat(129), voiceId: 'v', templateId: 't'})
    })).rejects.toThrow();
    await expect(readAcceptancePayload('/secret/payload', {
      stat: async () => ({mode: 0o100600}),
      readFile: async () => JSON.stringify({avatarId: 'real-avatar', voiceId: 'real-voice', templateId: 'real-template'})
    })).resolves.toEqual({avatarId: 'real-avatar', voiceId: 'real-voice', templateId: 'real-template'});
  });

  it('rejects group-readable files and parses only a Cookie header', async () => {
    await expect(readCredentialHeader(
      {kind: 'cookie', path: '/secret/cookie'},
      {stat: async () => ({mode: 0o100640}), readFile: async () => 'hq_session=secret'}
    )).rejects.toThrow('0600');
    await expect(readCredentialHeader(
      {kind: 'header', path: '/secret/header'},
      {stat: async () => ({mode: 0o100600}), readFile: async () => 'Authorization: Bearer secret'}
    )).rejects.toThrow('Cookie');
    await expect(readCredentialHeader(
      {kind: 'header', path: '/secret/header'},
      {stat: async () => ({mode: 0o100600}), readFile: async () => 'Cookie: hq_session=secret\n'}
    )).resolves.toEqual({cookie: 'hq_session=secret'});
  });

  it.each([0o100400, 0o100700])('requires exact 0600 permission bits (%o is rejected)', async (mode) => {
    await expect(readCredentialHeader(
      {kind: 'cookie', path: '/secret/cookie'},
      {stat: async () => ({mode}), readFile: async () => 'hq_session=secret'}
    )).rejects.toThrow('0600');
    await expect(readAcceptancePayload('/secret/payload', {
      stat: async () => ({mode}), readFile: async () => '{"avatarId":"a","voiceId":"v","templateId":"t"}'
    })).rejects.toThrow('0600');
  });
});
