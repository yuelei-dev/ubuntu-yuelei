import {describe, expect, it} from 'vitest';
import {POSTGRES_POOL_TIMEOUTS, createPostgresProjectRepository} from './drizzle-project-repository.js';

describe('PostgreSQL bounded-operation contract', () => {
  it('configures connect, query, statement, lock and idle-transaction deadlines', async () => {
    expect(POSTGRES_POOL_TIMEOUTS).toEqual({
      connectionTimeoutMillis: 5_000,
      query_timeout: 30_000,
      statement_timeout: 30_000,
      lock_timeout: 5_000,
      idle_in_transaction_session_timeout: 30_000
    });
    const {pool} = createPostgresProjectRepository('postgresql://u:p@127.0.0.1:5432/db');
    try {
      expect(pool.options).toMatchObject(POSTGRES_POOL_TIMEOUTS);
    } finally {
      await pool.end();
    }
  });
});
