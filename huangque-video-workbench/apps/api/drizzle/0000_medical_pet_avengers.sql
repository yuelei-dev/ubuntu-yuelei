CREATE TABLE "asset_versions" (
	"id" text PRIMARY KEY NOT NULL,
	"project_id" text NOT NULL,
	"scene_id" text NOT NULL,
	"version" integer NOT NULL,
	"uri" text NOT NULL,
	"provenance" text NOT NULL,
	"input_hash" text NOT NULL,
	"created_at" timestamp with time zone NOT NULL
);
--> statement-breakpoint
CREATE TABLE "jobs" (
	"id" text PRIMARY KEY NOT NULL,
	"project_id" text NOT NULL,
	"scene_id" text NOT NULL,
	"task_type" text NOT NULL,
	"input_hash" text NOT NULL,
	"options" jsonb,
	"status" text NOT NULL,
	"created_at" timestamp with time zone NOT NULL
);
--> statement-breakpoint
CREATE TABLE "projects" (
	"id" text PRIMARY KEY NOT NULL,
	"status" text NOT NULL,
	"title" text NOT NULL,
	"input" jsonb NOT NULL,
	"avatar" jsonb NOT NULL,
	"output" jsonb NOT NULL,
	"quality_report_path" text,
	"preview_url" text,
	"download_url" text,
	"created_at" timestamp with time zone NOT NULL,
	"updated_at" timestamp with time zone NOT NULL
);
--> statement-breakpoint
CREATE TABLE "scenes" (
	"id" text NOT NULL,
	"project_id" text NOT NULL,
	"scene_order" integer NOT NULL,
	"status" text NOT NULL,
	"script" text NOT NULL,
	"visual" jsonb NOT NULL,
	"asset" jsonb,
	"failure_reason" text,
	"created_at" timestamp with time zone NOT NULL,
	"updated_at" timestamp with time zone NOT NULL,
	CONSTRAINT "scenes_project_scene_unique" UNIQUE("project_id","id")
);
--> statement-breakpoint
ALTER TABLE "asset_versions" ADD CONSTRAINT "asset_versions_project_id_projects_id_fk" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "jobs" ADD CONSTRAINT "jobs_project_id_projects_id_fk" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "scenes" ADD CONSTRAINT "scenes_project_id_projects_id_fk" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE no action ON UPDATE no action;
