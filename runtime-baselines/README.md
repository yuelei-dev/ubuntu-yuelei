# Runtime baselines

Each `test/<content-id>/` directory is an immutable, content-addressed copy of
the allowlisted test-server runtime. The directory name must equal the
`content_id` in its `MANIFEST.json`.

The payload is deliberately stored outside the live `server/` and `site/`
source paths. It is audit evidence and a release input, not a branch base and
not an instruction to deploy. Environment values, credentials, certificates,
databases, logs, uploads, generated artifacts and user data are excluded.

CI verifies the complete payload against
`deploy/runtime-canonical/baseline-server.json`. Never edit an existing
content-id directory; create a new capture and a new directory instead.
