---
name: review-database-migration
description: Review database migrations, SQL, schema changes and transaction behavior. Use when a PR changes migrations, queries, indexes, constraints or data backfills.
metadata:
  capyreview-domains: correctness reliability
---

# Database Migration Review

## Workflow

1. Identify schema, data and application compatibility assumptions.
2. Check locking, transaction boundaries, rollback and partial-failure behavior.
3. Inspect callers when a constraint or column contract changes.
4. Distinguish deployment-order failures from query correctness defects.
5. Report only changed-line defects with exact evidence and a reproducible failure path.

## Tool guidance

- Use `search_repository` to find readers and writers of the changed schema.
- Use `read_file_history` to recover migration ordering and compatibility intent.
- Use `read_ci_failure` for migration or integration-test failures.

Read [migration safety](references/migration-safety.md) for rollout and rollback checks.
