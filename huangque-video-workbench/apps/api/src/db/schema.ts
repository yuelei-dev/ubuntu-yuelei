import {integer, jsonb, pgTable, text, timestamp, unique} from 'drizzle-orm/pg-core';
import type {ProjectInput, SceneRecord} from '../services/project-service.js';

const timestamps = {
  createdAt: timestamp('created_at', {withTimezone: true}).notNull(),
  updatedAt: timestamp('updated_at', {withTimezone: true}).notNull()
};

export const projects = pgTable('projects', {
  id: text('id').primaryKey(),
  ownerUsername: text('owner_username').notNull(),
  status: text('status').notNull(),
  title: text('title').notNull(),
  input: jsonb('input').$type<ProjectInput['input']>().notNull(),
  avatar: jsonb('avatar').$type<ProjectInput['avatar']>().notNull(),
  output: jsonb('output').$type<ProjectInput['output']>().notNull(),
  qualityReportPath: text('quality_report_path'),
  previewUrl: text('preview_url'),
  downloadUrl: text('download_url'),
  ...timestamps
});

export const scenes = pgTable('scenes', {
  id: text('id').notNull(),
  projectId: text('project_id').notNull().references(() => projects.id),
  order: integer('scene_order').notNull(),
  status: text('status').notNull(),
  script: text('script').notNull(),
  visual: jsonb('visual').$type<SceneRecord['visual']>().notNull(),
  asset: jsonb('asset').$type<NonNullable<SceneRecord['asset']>>(),
  failureReason: text('failure_reason'),
  ...timestamps
}, (table) => [
  unique('scenes_project_scene_unique').on(table.projectId, table.id)
]);

export const jobs = pgTable('jobs', {
  id: text('id').primaryKey(),
  projectId: text('project_id').notNull().references(() => projects.id),
  sceneId: text('scene_id').notNull(),
  taskType: text('task_type').notNull(),
  inputHash: text('input_hash').notNull(),
  dispatchPayload: jsonb('dispatch_payload').$type<unknown>(),
  options: jsonb('options').$type<NonNullable<import('../services/project-service.js').JobRecord['options']>>(),
  status: text('status').notNull(),
  renderLeaseOwner: text('render_lease_owner'),
  renderLeaseExpiresAt: timestamp('render_lease_expires_at', {withTimezone: true}),
  dispatchAttempts: integer('dispatch_attempts').notNull().default(0),
  nextDispatchAt: timestamp('next_dispatch_at', {withTimezone: true}).notNull().defaultNow(),
  dispatchLeaseOwner: text('dispatch_lease_owner'),
  dispatchLeaseExpiresAt: timestamp('dispatch_lease_expires_at', {withTimezone: true}),
  createdAt: timestamp('created_at', {withTimezone: true}).notNull()
});

export const assetVersions = pgTable('asset_versions', {
  id: text('id').primaryKey(),
  projectId: text('project_id').notNull().references(() => projects.id),
  sceneId: text('scene_id').notNull(),
  version: integer('version').notNull(),
  uri: text('uri').notNull(),
  provenance: text('provenance').notNull(),
  inputHash: text('input_hash').notNull(),
  createdAt: timestamp('created_at', {withTimezone: true}).notNull()
});
