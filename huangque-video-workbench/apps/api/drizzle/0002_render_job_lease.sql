ALTER TABLE "jobs" ADD COLUMN "render_lease_owner" text;--> statement-breakpoint
ALTER TABLE "jobs" ADD COLUMN "render_lease_expires_at" timestamp with time zone;
