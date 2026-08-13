# Concurrency and queue patterns

- Acknowledge work only after durable completion or durable requeue.
- Bound retries and preserve the final failure reason.
- Make duplicate delivery safe with idempotency keys or atomic state transitions.
- Reclaim expired leases without allowing two owners to commit the same effect.
- Persist checkpoints before acknowledging externally visible work.
