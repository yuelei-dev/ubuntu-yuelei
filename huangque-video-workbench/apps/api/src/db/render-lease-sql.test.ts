import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {describe, expect, it} from 'vitest';

describe('Drizzle render lease SQL authority', () => {
  it('uses PostgreSQL clock_timestamp for claim, renewal, and terminal fencing', async () => {
    const source = await readFile(resolve(process.cwd(), 'apps/api/src/db/drizzle-project-repository.ts'), 'utf8');
    const leaseSection = source.slice(source.indexOf('async claimRenderJobLease'), source.indexOf('async listAssetVersions'));
    expect(leaseSection).toContain('clock_timestamp()');
    expect(leaseSection).toContain("interval '1 millisecond'");
    expect(leaseSection).not.toContain('new Date(now)');
    expect(leaseSection).not.toContain('commit.now');
  });
});
