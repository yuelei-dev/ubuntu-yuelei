import {readFile, readdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {PGlite} from '@electric-sql/pglite';
import {describe, expect, it} from 'vitest';
import {SceneSchema} from '@huangque/contracts';
import {canonicalInputHash} from '../services/project-service.js';

const migrationDirectory = resolve(process.cwd(), 'apps', 'api', 'drizzle');

describe('Drizzle migrations', () => {
  it('contains a journaled baseline for every durable Task 4-10 field', async () => {
    const sqlFiles = (await readdir(migrationDirectory)).filter((file) => file.endsWith('.sql'));
    expect(sqlFiles).toHaveLength(5);

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
    const ownershipMigration = await readFile(resolve(migrationDirectory, '0001_project_ownership.sql'), 'utf8');
    expect(ownershipMigration).toContain('\"owner_username\"');
    expect(ownershipMigration).toContain('NOT NULL');
    const renderLeaseMigration = await readFile(resolve(migrationDirectory, '0002_render_job_lease.sql'), 'utf8');
    expect(renderLeaseMigration).toContain('render_lease_owner');
    expect(renderLeaseMigration).toContain('render_lease_expires_at');
    const outboxMigration = await readFile(resolve(migrationDirectory, '0004_outbox_retry.sql'), 'utf8');
    for (const column of [
      'dispatch_payload', 'dispatch_attempts', 'next_dispatch_at',
      'dispatch_lease_owner', 'dispatch_lease_expires_at'
    ]) expect(outboxMigration).toContain(column);

    const journal = JSON.parse(await readFile(resolve(migrationDirectory, 'meta', '_journal.json'), 'utf8')) as {entries?: unknown[]};
    expect(journal.entries).toHaveLength(5);
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

  it('quarantines legacy pending control and render jobs without immutable payloads', async () => {
    const database = new PGlite();
    try {
      for (const file of ['0000_medical_pet_avengers.sql', '0001_project_ownership.sql', '0002_render_job_lease.sql', '0003_canonical_scene_revision.sql']) {
        const migration = await readFile(resolve(migrationDirectory, file), 'utf8');
        for (const statement of migration.split('--> statement-breakpoint')) {
          if (statement.trim()) await database.exec(statement);
        }
      }
      await database.exec(`INSERT INTO projects (id, owner_username, status, title, input, avatar, output, created_at, updated_at)
        VALUES ('legacy_outbox', 'alice', 'RENDERING', 'Legacy', '{"type":"script","content":"legacy"}', '{"avatarId":"a","voiceId":"v"}', '{"templateId":"t"}', now(), now())`);
      await database.exec(`INSERT INTO jobs (id, project_id, scene_id, task_type, input_hash, status, created_at) VALUES
        ('legacy-regenerate', 'legacy_outbox', 'scene-1', 'scene.regenerate', 'a', 'PENDING', now()),
        ('legacy-render', 'legacy_outbox', 'project', 'project.render', 'b', 'PENDING', now())`);
      const migration = await readFile(resolve(migrationDirectory, '0004_outbox_retry.sql'), 'utf8');
      for (const statement of migration.split('--> statement-breakpoint')) {
        if (statement.trim()) await database.exec(statement);
      }
      const rows = await database.query<{id: string; dispatch_payload: unknown; quarantined: boolean; options: Record<string, unknown>}>(
        `select id, dispatch_payload, next_dispatch_at = 'infinity'::timestamp with time zone as quarantined, options from jobs order by id`
      );
      expect(rows.rows).toHaveLength(2);
      for (const row of rows.rows) {
        expect(row.dispatch_payload).toBeNull();
        expect(row.quarantined).toBe(true);
        expect(row.options.outboxQuarantineReason).toContain('no immutable dispatch payload');
      }
      const claimable = await database.query<{count: number}>(
        `select count(*)::integer as count from jobs
         where status = 'PENDING' and dispatch_payload is not null and next_dispatch_at <= clock_timestamp()`
      );
      expect(claimable.rows).toEqual([{count: 0}]);
    } finally {
      await database.close();
    }
  });

  it('backfills legacy projects to the non-claimable quarantine owner', async () => {
    const database = new PGlite();
    try {
      const baseline = await readFile(resolve(migrationDirectory, '0000_medical_pet_avengers.sql'), 'utf8');
      for (const statement of baseline.split('--> statement-breakpoint')) {
        if (statement.trim()) await database.exec(statement);
      }
      await database.exec(`INSERT INTO projects (id, status, title, input, avatar, output, created_at, updated_at)
        VALUES ('legacy_project', 'COMPLETED', 'Legacy', '{"type":"script","content":"legacy"}', '{"avatarId":"a","voiceId":"v"}', '{"templateId":"t"}', now(), now())`);
      const ownership = await readFile(resolve(migrationDirectory, '0001_project_ownership.sql'), 'utf8');
      for (const statement of ownership.split('--> statement-breakpoint')) {
        if (statement.trim()) await database.exec(statement);
      }

      const projects = await database.query<{owner_username: string}>(`select owner_username from projects where id = 'legacy_project'`);
      const columns = await database.query<{is_nullable: string}>(`select is_nullable from information_schema.columns where table_name = 'projects' and column_name = 'owner_username'`);
      expect(projects.rows).toEqual([{owner_username: '__huangque_legacy_unowned__'}]);
      expect(columns.rows).toEqual([{is_nullable: 'NO'}]);
    } finally {
      await database.close();
    }
  });

  it('backfills pre-canonical workerScene rows into parseable regeneration and render inputs without dropping metadata', async () => {
    const database = new PGlite();
    try {
      const sqlFiles = (await readdir(migrationDirectory)).filter((file) => file.endsWith('.sql')).sort();
      const canonicalMigration = sqlFiles.find((file) => file === '0003_canonical_scene_revision.sql');
      expect(canonicalMigration).toBeDefined();
      if (!canonicalMigration) return;
      for (const file of sqlFiles.filter((file) => file !== canonicalMigration)) {
        const migration = await readFile(resolve(migrationDirectory, file), 'utf8');
        for (const statement of migration.split('--> statement-breakpoint')) {
          if (statement.trim()) await database.exec(statement);
        }
      }
      await database.exec(`INSERT INTO projects (id, owner_username, status, title, input, avatar, output, created_at, updated_at)
        VALUES ('legacy_scene_project', 'alice', 'COMPLETED', 'Legacy', '{"type":"script","content":"legacy"}', '{"avatarId":"a","voiceId":"v"}', '{"templateId":"t"}', now(), now())`);
      await database.exec(`INSERT INTO scenes (id, project_id, scene_order, status, script, visual, created_at, updated_at)
        VALUES ('scene_001', 'legacy_scene_project', 1, 'READY', 'Edited narration',
        '{"layout":"visual_full","headline":"Current headline","highlightWords":[],"visualPrompt":"Edited direction","activeGenerationJobId":"generation_1","attempt":2,"workerScene":{"id":"scene_001","order":1,"type":"image","purpose":"visual","script":"Stale narration","durationEstimate":2,"visual":{"layout":"visual_full","highlightWords":[]}}}', now(), now())`);
      await database.exec(`INSERT INTO scenes (id, project_id, scene_order, status, script, visual, created_at, updated_at)
        VALUES ('scene_unsafe', 'legacy_scene_project', 2, 'READY', 'Unchanged',
        '{"layout":"visual_full","highlightWords":[],"activeGenerationJobId":"generation_2","workerScene":{"id":"scene_unsafe"}}', now(), now())`);
      await database.exec(`INSERT INTO scenes (id, project_id, scene_order, status, script, visual, created_at, updated_at)
        VALUES
        ('scene_bad_type', 'legacy_scene_project', 3, 'READY', 'Unchanged', '{"workerScene":{"type":"unsafe","purpose":"visual","durationEstimate":2}}', now(), now()),
        ('scene_bad_duration', 'legacy_scene_project', 4, 'READY', 'Unchanged', '{"workerScene":{"type":"avatar","purpose":"intro","durationEstimate":13}}', now(), now()),
        ('scene_bad_purpose', 'legacy_scene_project', 5, 'READY', 'Unchanged', '{"workerScene":{"type":"image","purpose":"","durationEstimate":2}}', now(), now()),
        ('scene_bad_revision', 'legacy_scene_project', 6, 'READY', 'Unchanged', '{"contentRevision":999999999999999999999999999,"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', now(), now())`);
      await database.exec(`INSERT INTO scenes (id, project_id, scene_order, status, script, visual, asset, created_at, updated_at)
        VALUES
        ('scene_bad_layout', 'legacy_scene_project', 7, 'READY', 'Unchanged', '{"layout":"","highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_bad_headline', 'legacy_scene_project', 8, 'READY', 'Unchanged', '{"layout":"visual_full","headline":2,"highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_bad_highlights', 'legacy_scene_project', 9, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[2],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_bad_prompt', 'legacy_scene_project', 10, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[],"visualPrompt":"","workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_bad_asset', 'legacy_scene_project', 11, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', '{"source":2}', now(), now()),
        ('', 'legacy_scene_project', 12, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_bad_order', 'legacy_scene_project', 0, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_bad_script', 'legacy_scene_project', 14, 'READY', '', '{"layout":"visual_full","highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":2}}', null, now(), now()),
        ('scene_numeric_purpose', 'legacy_scene_project', 15, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[],"workerScene":{"type":"image","purpose":2,"durationEstimate":2}}', null, now(), now()),
        ('scene_string_duration', 'legacy_scene_project', 16, 'READY', 'Unchanged', '{"layout":"visual_full","highlightWords":[],"workerScene":{"type":"image","purpose":"visual","durationEstimate":"2"}}', null, now(), now())`);

      const migration = await readFile(resolve(migrationDirectory, canonicalMigration), 'utf8');
      for (const statement of migration.split('--> statement-breakpoint')) {
        if (statement.trim()) await database.exec(statement);
      }
      const scene = await database.query<{script: string; scene_order: number; visual: Record<string, unknown>}>(`select script, scene_order, visual from scenes where id = 'scene_001'`);
      expect(scene.rows[0]?.visual).toMatchObject({
        sceneType: 'image', purpose: 'visual', durationEstimate: 2, contentRevision: 0,
        layout: 'visual_full', headline: 'Current headline', visualPrompt: 'Edited direction', activeGenerationJobId: 'generation_1', attempt: 2
      });
      expect(scene.rows[0]?.visual.workerScene).toBeUndefined();
      const parsed = SceneSchema.parse({
        id: 'scene_001', order: scene.rows[0]?.scene_order, type: scene.rows[0]?.visual.sceneType,
        purpose: scene.rows[0]?.visual.purpose, script: scene.rows[0]?.script,
        durationEstimate: scene.rows[0]?.visual.durationEstimate, visualPrompt: scene.rows[0]?.visual.visualPrompt,
        visual: {layout: scene.rows[0]?.visual.layout, headline: scene.rows[0]?.visual.headline, highlightWords: scene.rows[0]?.visual.highlightWords}
      });
      expect(canonicalInputHash({taskType: 'scene.asset.generate', scene: {...parsed, contentRevision: scene.rows[0]?.visual.contentRevision}})).toMatch(/^[a-f0-9]{64}$/);
      expect(canonicalInputHash({output: {templateId: 't'}, scenes: [{script: parsed.script, visual: parsed.visual, visualPrompt: parsed.visualPrompt}]})).toMatch(/^[a-f0-9]{64}$/);
      const unsafe = await database.query<{visual: Record<string, unknown>}>(`select visual from scenes where id = 'scene_unsafe'`);
      expect(unsafe.rows[0]?.visual).toMatchObject({contentRevision: 0, activeGenerationJobId: 'generation_2', workerScene: {id: 'scene_unsafe'}});
      for (const id of [
        'scene_bad_type', 'scene_bad_duration', 'scene_bad_purpose', 'scene_bad_revision',
        'scene_bad_layout', 'scene_bad_headline', 'scene_bad_highlights', 'scene_bad_prompt',
        'scene_bad_asset', '', 'scene_bad_order', 'scene_bad_script',
        'scene_numeric_purpose', 'scene_string_duration'
      ]) {
        const invalid = await database.query<{visual: Record<string, unknown>}>(`select visual from scenes where id = '${id}'`);
        expect(invalid.rows[0]?.visual, `legacy row ${JSON.stringify(id)}`).toMatchObject({
          workerScene: expect.any(Object), canonicalSceneQuarantine: expect.any(String)
        });
      }
    } finally {
      await database.close();
    }
  });
});
