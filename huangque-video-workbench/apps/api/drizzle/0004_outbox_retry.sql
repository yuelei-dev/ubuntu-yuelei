ALTER TABLE "jobs" ADD COLUMN "dispatch_attempts" integer DEFAULT 0 NOT NULL;
ALTER TABLE "jobs" ADD COLUMN "dispatch_payload" jsonb;
ALTER TABLE "jobs" ADD COLUMN "next_dispatch_at" timestamp with time zone DEFAULT now() NOT NULL;
ALTER TABLE "jobs" ADD COLUMN "dispatch_lease_owner" text;
ALTER TABLE "jobs" ADD COLUMN "dispatch_lease_expires_at" timestamp with time zone;
UPDATE "jobs"
SET "next_dispatch_at" = 'infinity'::timestamp with time zone,
    "options" = coalesce("options", '{}'::jsonb) ||
      '{"outboxQuarantineReason":"legacy PENDING job has no immutable dispatch payload; follow docs/operations/outbox-recovery.md"}'::jsonb
WHERE "status" = 'PENDING' AND "dispatch_payload" IS NULL;
CREATE INDEX "jobs_outbox_due_idx" ON "jobs" ("next_dispatch_at", "created_at")
  WHERE "status" = 'PENDING' AND "dispatch_payload" IS NOT NULL;
