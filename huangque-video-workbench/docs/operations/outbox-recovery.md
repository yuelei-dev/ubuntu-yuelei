# Outbox quarantine recovery

Migration `0004_outbox_retry.sql` quarantines legacy `PENDING` jobs that do not
contain the immutable payload required to prove what should be sent to BullMQ.
They remain `PENDING`, have `next_dispatch_at = 'infinity'`, and carry an
`outboxQuarantineReason` in `options`. The dispatcher also requires
`dispatch_payload IS NOT NULL`, so these rows cannot be submitted accidentally.

Do not reconstruct a payload from current mutable project or scene data without
verification. For a recoverable row:

1. Obtain the original request payload from an authoritative audit/backup.
2. Canonicalize it with the same `canonicalInputHash` implementation used by
   the application.
3. Confirm that the resulting SHA-256 equals the row's `input_hash`.
4. In one database transaction, set `dispatch_payload` to the verified JSON,
   set `next_dispatch_at = clock_timestamp()`, reset `dispatch_attempts` to
   zero, clear both dispatch lease columns, and remove
   `outboxQuarantineReason` from `options`.
5. Observe the dispatcher enqueue the row and verify that its state becomes
   `QUEUED`.

If the original payload cannot be proven, leave the row quarantined and create
a new user-authorized request through the application after an operator has
resolved the obsolete deterministic job identity. Never make a null-payload row
claimable merely to clear the queue.
