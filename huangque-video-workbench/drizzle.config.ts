import {defineConfig} from 'drizzle-kit';

export default defineConfig({
  dialect: 'postgresql',
  schema: './apps/api/src/db/schema.ts',
  out: './apps/api/drizzle',
  dbCredentials: {
    url: process.env.DATABASE_URL ?? 'postgresql://huangque:localdev@127.0.0.1:5432/huangque'
  }
});
