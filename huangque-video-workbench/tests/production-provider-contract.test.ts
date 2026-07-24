import {readFile} from 'node:fs/promises';
import {describe, expect, it} from 'vitest';

describe('production provider composition contract', () => {
  it('contains no fixture or mock imports and requires all external endpoints', async () => {
    const worker = await readFile('scripts/docker-worker.ts', 'utf8');
    expect(worker).not.toMatch(/tests\/fixtures|MockAvatar|MockImage|renderFixtureVideo|assertFixtureVideoVisuals|start-local-composition|video-visual-analysis/);
    expect(worker).toContain('HttpAvatarProvider');
    expect(worker).toContain('HttpImageProvider');
    expect(worker).toContain('requestProductionRender');
    expect(worker).toContain('RenderProviderResponseSchema.parse');
    expect(worker).not.toContain('output.arrayBuffer()');
    expect(worker).toContain('output.body.getReader()');

    const compose = await readFile('infra/docker-compose.yml', 'utf8');
    for (const name of ['AVATAR_PROVIDER_ENDPOINT', 'IMAGE_PROVIDER_ENDPOINT', 'RENDER_PROVIDER_ENDPOINT', 'PROVIDER_TOKEN', 'PROVIDER_ALLOWED_MEDIA_ORIGINS']) {
      expect(compose).toContain(`${name}: \${${name}:?`);
    }
  });

  it('does not copy tests, fixtures, or mock providers into the production image', async () => {
    const dockerfile = await readFile('infra/Dockerfile', 'utf8');
    expect(dockerfile).not.toContain('COPY . .');
    expect(dockerfile).not.toMatch(/COPY\s+tests/);
    expect(dockerfile).toContain('rm -rf ./packages/providers/src/mock');
    expect(dockerfile).toContain("-name '*.test.tsx'");
    expect(dockerfile).toContain("-name '*.spec.tsx'");
    expect(dockerfile).toContain('FROM node:22-bookworm-slim AS application-source');
    expect(dockerfile).toContain('COPY --from=application-source /app /app');
  });

  it('does not export deterministic providers from the production package entry point', async () => {
    const entry = await readFile('packages/providers/src/index.ts', 'utf8');
    expect(entry).not.toMatch(/mock|development|fixture/i);
    expect(entry).toContain("export * from './http.js'");
  });
});
