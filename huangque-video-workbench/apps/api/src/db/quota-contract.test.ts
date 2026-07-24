import {readFile} from 'node:fs/promises';
import {describe, expect, it} from 'vitest';

describe('production owner quota SQL contract', () => {
  it('serializes the count and inserts in one transaction-scoped advisory lock', async () => {
    const source = await readFile('apps/api/src/db/drizzle-project-repository.ts', 'utf8');
    const method = source.slice(
      source.indexOf('async createProjectWithInitialJobQuota'),
      source.indexOf('async findProject(', source.indexOf('async createProjectWithInitialJobQuota'))
    );
    expect(method).toContain('this.transaction');
    expect(method).toContain('pg_advisory_xact_lock');
    expect(method).toContain('ownerUsername');
    expect(method).toContain('count(*)::int');
    expect(method).toContain('await this.createProject(project)');
    expect(method).toContain('await this.reserveJob(job)');
  });
});
