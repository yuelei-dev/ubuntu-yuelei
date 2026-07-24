import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {describe, expect, it} from 'vitest';

type Location = {modifier: string; path: string; body: string};

const locationsIn = (source: string): Location[] => {
  const clean = source.replace(/#.*$/gm, '');
  const locations: Location[] = [];
  const pattern = /\blocation\s+(?:(=)\s+)?(\S+)\s*\{/g;
  for (let match = pattern.exec(clean); match;) {
    let depth = 1;
    let cursor = pattern.lastIndex;
    while (cursor < clean.length && depth > 0) {
      if (clean[cursor] === '{') depth += 1;
      if (clean[cursor] === '}') depth -= 1;
      cursor += 1;
    }
    if (depth !== 0) throw new Error(`unclosed location ${match[2]}`);
    locations.push({modifier: match[1] ?? '', path: match[2]!, body: clean.slice(pattern.lastIndex, cursor - 1)});
    pattern.lastIndex = cursor;
    match = pattern.exec(clean);
  }
  return locations;
};

const assertContract = (source: string): void => {
  const locations = locationsIn(source);
  expect(locations.map(({modifier, path}) => `${modifier}:${path}`)).toEqual([
    '=:/video-workbench',
    ':/video-workbench/',
    '=:/api/video-workbench',
    ':/api/video-workbench/'
  ]);
  const uiRedirect = locations[0]!;
  const ui = locations[1]!;
  const apiRedirect = locations[2]!;
  const api = locations[3]!;
  expect(uiRedirect.body).toMatch(/^\s*return 308 \/video-workbench\/;\s*$/);
  expect(apiRedirect.body).toMatch(/^\s*return 308 \/api\/video-workbench\/;\s*$/);
  expect(ui.body).toContain('proxy_pass http://127.0.0.1:4173/;');
  expect(api.body).toContain('proxy_pass http://127.0.0.1:4173/api/;');
  for (const block of [ui, api]) {
    expect(block.body).toContain('proxy_http_version 1.1;');
    expect(block.body).toContain('proxy_buffering off;');
    expect(block.body).toContain('proxy_cache off;');
    expect(block.body).toContain('proxy_read_timeout 3600s;');
    expect(block.body).toContain('proxy_send_timeout 3600s;');
    expect(block.body).toContain('client_max_body_size 100m;');
    expect(block.body).toContain('proxy_set_header Host $host;');
    expect(block.body).toContain('proxy_set_header X-Forwarded-Proto $scheme;');
    expect(block.body).toContain('proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;');
    expect(block.body).toContain('add_header X-Content-Type-Options "nosniff" always;');
    expect(block.body).toContain('add_header Referrer-Policy "same-origin" always;');
    expect(block.body).toContain('add_header X-Frame-Options "SAMEORIGIN" always;');
    expect(block.body).not.toMatch(/Access-Control-Allow-Origin\s+"\*"/);
  }
  expect(source.replace(/#.*$/gm, '')).not.toMatch(/\b(?:0\.0\.0\.0|\[::\]|localhost)\b/);
  expect(source.replace(/#.*$/gm, '')).not.toContain('upstream ');
};

describe('production Nginx contract', () => {
  it('maps the UI root and API mount to the actual upstream route trees', async () => {
    assertContract(await readFile(resolve('infra', 'nginx-video-workbench.conf'), 'utf8'));
  });

  it.each([
    ['API prefix not restored upstream', (source: string) => source.replace('4173/api/;', '4173/;')],
    ['SSE directive moved to a comment', (source: string) => source.replace('proxy_buffering off;', '# proxy_buffering off;')],
    ['directive moved outside its location', (source: string) => source.replace('    client_max_body_size 100m;', '').concat('\nclient_max_body_size 100m;\n')],
    ['extra public location', (source: string) => source.concat('\nlocation /oops/ { proxy_pass http://127.0.0.1:4173/; }\n')]
  ])('rejects weakening mutation: %s', async (_name, mutate) => {
    const source = await readFile(resolve('infra', 'nginx-video-workbench.conf'), 'utf8');
    expect(() => assertContract(mutate(source))).toThrow();
  });
});
