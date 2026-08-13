# Migration safety

- Prefer expand-and-contract for incompatible schema changes.
- Avoid long table locks and unbounded backfills in a single transaction.
- Make retries idempotent and define rollback behavior.
- Preserve mixed-version application compatibility during deployment.
- Add constraints only after existing data satisfies them.
