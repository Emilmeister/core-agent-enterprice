"""Explicit operator attestation after infrastructure has stopped an executor."""
import json
import os
import stat

from .errors import CoreError
from .workflow import PostgresWorkflowStore


def read_stop_confirmation(path, tenant):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError()
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        keys = {"version", "tenant_id", "owner_id", "run_id", "instance_id", "worker_id", "generation", "evidence"}
        if not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int or value["version"] != 1:
            raise ValueError()
        if not tenant or value["tenant_id"] != tenant:
            raise ValueError()
        if any(not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 1024
               or any(ord(c) < 32 for c in value[key]) for key in keys - {"version"}):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise CoreError("CONFIG_INVALID", "Invalid execution stop confirmation") from None


def confirm_operator_stop(database, confirmation):
    store = PostgresWorkflowStore(database)
    record = store.get(confirmation["run_id"], tenant_id=confirmation["tenant_id"], owner_id=confirmation["owner_id"])
    with database.transaction() as connection:
        actor = connection.execute("SELECT current_user AS actor").fetchone()["actor"]
    return store.confirm_execution(record, confirmation, strict=True, proof={
        "kind": "operator", "actor": f"operator:{actor}", "evidence": confirmation["evidence"],
    })
