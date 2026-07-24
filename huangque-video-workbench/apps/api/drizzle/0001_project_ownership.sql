ALTER TABLE "projects" ADD COLUMN "owner_username" text;
--> statement-breakpoint
UPDATE "projects" SET "owner_username" = '__huangque_legacy_unowned__' WHERE "owner_username" IS NULL;
--> statement-breakpoint
ALTER TABLE "projects" ALTER COLUMN "owner_username" SET NOT NULL;
