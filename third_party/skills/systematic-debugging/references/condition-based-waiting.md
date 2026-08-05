# Condition-based waiting

Use readiness or completion conditions instead of arbitrary sleeps:

1. Name the state that proves the operation is ready or complete.
2. Prefer a native notification, watch, task wait, health endpoint, or durable
   status query.
3. If polling is unavoidable, use a bounded interval, a hard deadline, and a
   passive wait between checks. Do not busy-poll.
4. On timeout, report the last observed state and elapsed deadline. A timeout is
   not proof that the underlying operation failed.
5. Keep a fixed delay only when elapsed time itself is the behavior under test,
   and explain the timing relationship.

For Kubernetes or scale-to-zero services, distinguish deployment readiness,
network reachability, protocol initialization, and application readiness; each
is a separate condition.
