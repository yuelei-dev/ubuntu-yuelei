import {spawnSync} from 'node:child_process';
import {resolve} from 'node:path';

if (!process.env.TEST_POSTGRES_DATABASE_URL) {
  console.error('TEST_POSTGRES_DATABASE_URL is required for the mandatory PostgreSQL integration test.');
  process.exit(2);
}

const vitest = resolve('node_modules', 'vitest', 'vitest.mjs');
const result = spawnSync(process.execPath, [
  vitest,
  'run',
  'apps/api/src/outbox/postgres-dispatcher.integration.test.ts'
], {
  cwd: process.cwd(),
  env: process.env,
  stdio: 'inherit'
});

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
