# Execution recovery after container restart

Approved scope: preserve the terminal cleanup barrier, recover stopped execution
hosts, show reconciliation in the owner UI, and repair the two reported local chats.

1. Persist a private container/lifetime identity with each execution generation.
   Only a verified restart of the same container slot proves old local processes
   stopped. Different replicas, missing identity, and expired leases do not.
2. Apply that proof before resuming an attempt and before committing a terminal
   intent, including canonical children. Preserve unknown side-effect outcomes;
   never replay an interrupted mutating tool.
3. Provide an explicit operator command accepting exact tenant/run/instance/
   generation selections after infrastructure confirms termination. Record the
   evidence with the cleanup receipt; validate one exact selection before applying.
4. Project a finite cleanup-pending marker without exposing private identities.
   Show a clear interruption status and suppress typing animation until settled.
5. Verify positive and negative restart cases, stale receipts, tenant isolation,
   cancellation, projection, and a real Docker restart. Update the local image,
   confirm old executors are stopped, and repair the reported chats through the
   receipt path. Keep their history/files; let the owner delete the chats.

Docker identity is obtained from runtime-owned mounts, never HOSTNAME. Kubernetes
uses trusted downward-API Pod UID and a fixed container name, with a dedicated
PID namespace and the application running as PID 1. Other deployments and
replacement containers use operator reconciliation; no Docker socket, host PID
namespace, Kubernetes API credentials, or new background service are added.
