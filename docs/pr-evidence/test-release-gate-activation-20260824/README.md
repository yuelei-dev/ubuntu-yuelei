# Test release gate activation evidence

- Required check name: `Trusted test release impact gate`
- Expected GitHub App ID: `4709038`
- `main` branch protection uses `strict=true`.
- In spoof-drill PR #297, a same-name successful check from the `github-actions` App (ID `15368`) did not replace the dedicated App failure, and the PR remained `BLOCKED`.
- This docs-only PR safely advances the Base so the prior PR #292 result becomes stale and must be revalidated against the latest Base.

This evidence contains no credentials or secrets and makes no workflow, runtime, deployment, or business-code change.
