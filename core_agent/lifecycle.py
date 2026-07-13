from __future__ import annotations

import time

from psycopg.types.json import Jsonb

from .errors import CoreError


class PostgresRetentionManager:
    def __init__(self, database, artifact_store, clock=time.time):
        self.database = database
        self.artifact_store = artifact_store
        self.clock = clock

    def delete_run(self, tenant_id, run_id, *, operator_principal_id):
        now = self.clock()
        with self.database.transaction() as connection:
            root = connection.execute(
                """SELECT run_id FROM core_runs
                   WHERE run_id = %s AND tenant_id = %s FOR UPDATE""",
                (run_id, tenant_id),
            ).fetchone()
            if not root:
                raise CoreError("TASK_NOT_FOUND")
            rows = connection.execute(
                """WITH RECURSIVE family AS (
                     SELECT run_id, task_id FROM core_runs
                     WHERE run_id = %s AND tenant_id = %s
                     UNION ALL
                     SELECT child.run_id, child.task_id FROM core_runs child
                     JOIN family parent ON child.parent_run_id = parent.run_id
                     WHERE child.tenant_id = %s
                   ) SELECT run_id, task_id FROM family""",
                (run_id, tenant_id, tenant_id),
            ).fetchall()
            run_ids = [row["run_id"] for row in rows]
            task_ids = [row["task_id"] for row in rows]
            digests = [
                row["digest"]
                for row in connection.execute(
                    """UPDATE core_artifacts SET state = 'deleted', deleted_at = %s
                       WHERE tenant_id = %s AND state = 'active'
                         AND provenance->>'run_id' = ANY(%s)
                       RETURNING digest""",
                    (now, tenant_id, run_ids),
                ).fetchall()
            ]
            for current_run_id in run_ids:
                sequence = connection.execute(
                    """SELECT COALESCE(max(sequence), 0) + 1 AS sequence
                       FROM core_audit_records
                       WHERE run_id = %s AND tenant_id = %s""",
                    (current_run_id, tenant_id),
                ).fetchone()["sequence"]
                connection.execute(
                    """INSERT INTO core_audit_records
                       (run_id, sequence, kind, data, written_at, tenant_id)
                       VALUES (%s, %s, 'retention.deleted', %s, %s, %s)""",
                    (
                        current_run_id,
                        sequence,
                        Jsonb(
                            {
                                "content_deleted": True,
                                "operator_principal_id": operator_principal_id,
                            }
                        ),
                        now,
                        tenant_id,
                    ),
                )
            connection.execute(
                "DELETE FROM core_notifications WHERE tenant_id = %s AND owner_run_id = ANY(%s)",
                (tenant_id, run_ids),
            )
            connection.execute(
                "DELETE FROM core_background_tasks WHERE tenant_id = %s AND owner_run_id = ANY(%s)",
                (tenant_id, run_ids),
            )
            connection.execute(
                """DELETE FROM core_outbox WHERE tenant_id = %s
                   AND (aggregate_id = ANY(%s) OR aggregate_id = ANY(%s))""",
                (tenant_id, run_ids, task_ids),
            )
            connection.execute(
                "DELETE FROM core_checkpoints WHERE tenant_id = %s AND run_id = ANY(%s)",
                (tenant_id, run_ids),
            )
            connection.execute(
                "DELETE FROM core_events WHERE tenant_id = %s AND run_id = ANY(%s)",
                (tenant_id, run_ids),
            )
            connection.execute(
                "DELETE FROM core_a2a_tasks WHERE tenant = %s AND task_id = ANY(%s)",
                (tenant_id, task_ids),
            )
            connection.execute(
                "DELETE FROM core_runs WHERE tenant_id = %s AND run_id = ANY(%s)",
                (tenant_id, run_ids),
            )
            connection.execute(
                "DELETE FROM core_budget_ledgers WHERE tenant_id = %s AND root_run_id = %s",
                (tenant_id, run_id),
            )
        self.artifact_store.purge_unreferenced(digests)
        return {"deleted": True, "runs": len(run_ids), "artifacts": len(digests)}
