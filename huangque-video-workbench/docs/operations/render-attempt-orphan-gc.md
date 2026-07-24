# Render attempt orphan cleanup

Every render artifact is written under an immutable owner-token attempt prefix. A failed fence or aborted uninterruptible SDK call can leave an unreferenced orphan without changing the project-visible winner.

Run a scheduled retention job at least daily:

1. Read durable projects and retain the attempt token referenced by each project's preview/report keys.
2. List `projects/<project-id>/attempts/` in MinIO and local run directories older than the retention window (24 hours by default).
3. Pass only non-winner attempt IDs and their exact local paths/object keys to `cleanupKnownLosingAttempts`.
4. Use a per-delete deadline, record timeouts, and retry them on the next run. Never derive a delete target from a winner attempt token.

The helper intentionally does not list or delete broadly by prefix itself: enumeration and retention policy belong to the scheduled operator with durable project context.
