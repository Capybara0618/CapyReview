---
name: review-async-reliability
description: Review asynchronous jobs, queues, retries, locks and concurrent state transitions. Use when a PR changes async execution, workers, Redis Streams, retries, leases, locks or idempotency.
metadata:
  capyreview-domains: correctness reliability regression
  capyreview-signals: async queue worker retry lease lock idempotency stream concurrent celery
---

# Asynchronous Reliability Review

## Workflow

1. Trace task ownership, acknowledgement and terminal state transitions.
2. Check retry bounds, idempotency and duplicate-delivery behavior.
3. Inspect cancellation, timeout, lease recovery and partial failures.
4. Verify shared state updates are atomic or consistently locked.
5. Report a concrete interleaving or failure path with exact changed-line evidence.

## Tool guidance

- Use `search_repository` to find producers, consumers and state writers.
- Use `read_code_context` around acknowledgement and retry transitions.
- Use `read_ci_failure` for race, timeout and integration-test evidence.

Read [concurrency patterns](references/concurrency-patterns.md) for queue and locking checks.
