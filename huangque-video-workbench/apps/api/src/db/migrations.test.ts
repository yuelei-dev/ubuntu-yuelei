import {readFile, readdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {PGlite} from '@electric-sql/pglite';
import {describe, expect, it} from 'vitest';

const migrationDirectory = resolve(process.cwd(), 'apps', 'api', 'drizzle');

describe('Drizzle migrations', () => {
  it('contains a journaled baseline for every durable Task 4-10 field', async () => {
    const sqlFiles = (await readdir(migrationDirectory)).filter((file) => file.endsWith('.sql'));
    expect(sqlFiles).toHaveLength(1);

    const sql = await readFile(resolve(migrationDirectory, sqlFiles[0]!), 'utf8');
    for (const table of ['projects', 'scenes', 'jobs', 'asset_versions']) {
      expect(sql).toContain(`CREATE TABLE \"${table}\"`);
    }
    for (const column of [
      'quality_report_path', 'preview_url', 'download_url', 'failure_reason', 'options'
    ]) {
      expect(sql).toContain(`\"${column}\"`);
    }
    expect(sql).toContain('CONSTRAINT \"scenes_project_scene_unique\" UNIQUE');

    const journal = JSON.parse(await readFile(resolve(migrationDirectory, 'meta', '_journal.json'), 'utf8')) as {entries?: unknown[]};
    expect(journal.entries).toHaveLength(1);
  });

  it('applies cleanly and creates the expected PostgreSQL schema', async () => {
    const database = new PGlite();
    try {
      const sqlFiles = (await readdir(migrationDirectory)).filter((file) => file.endsWith('.sql')).sort();
      for (const file of sqlFiles) {
        const migration = await readFile(resolve(migrationDirectory, file), 'utf8');
        for (const statement of migration.split('--> statement-breakpoint')) {
          if (statement.trim()) await database.exec(statement);
        }
      }

      const tables = await database.query<{table_name: string}>(
        `select table_name from information_schema.tables where table_schema = 'public' order by table_name`
      );
      expect(tables.rows.map((row) => row.table_name)).toEqual(['asset_versions', 'jobs', 'projects', 'scenes']);

      const constraints = await database.query<{constraint_name: string}>(
        `select constraint_name from information_schema.table_constraints where table_schema = 'public'`
      );
      expect(constraints.rows.map((row) => row.constraint_name)).toContain('scenes_project_scene_unique');
    } finally {
      await database.close();
    }
  });
});
