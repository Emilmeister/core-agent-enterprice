from __future__ import annotations

import argparse
import asyncio
import os
import time
from contextlib import contextmanager, nullcontext

from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import TaskStore
from a2a.types import a2a_pb2
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token
from psycopg import Error as PostgresError
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from google.protobuf.json_format import MessageToDict

from .audit import AuditRecord
from .a2a import workflow_result_artifact
from .auth import ScopeUser, is_company_owner
from .durability import Event
from .errors import CoreError
from .tasks import REMOTE_PROGRESS_STATES


SCHEMA_VERSION = 24
MIGRATIONS = {
    1: """
CREATE TABLE IF NOT EXISTS core_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core_tool_proposals (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    context_id text NOT NULL,
    tenant_id text NOT NULL,
    caller_principal_id text NOT NULL,
    tool_call_id text NOT NULL,
    tool_name text NOT NULL,
    tool_version text NOT NULL,
    environment text NOT NULL,
    target_json text NOT NULL,
    arguments_json text NOT NULL,
    side_effect_class text NOT NULL,
    risk_level text NOT NULL,
    policy_version text NOT NULL,
    created_at double precision NOT NULL,
    action_digest text NOT NULL
);

CREATE TABLE core_approval_requests (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    proposal_id text NOT NULL UNIQUE REFERENCES core_tool_proposals(id),
    action_digest text NOT NULL,
    state text NOT NULL,
    version integer NOT NULL,
    created_at double precision NOT NULL,
    expires_at double precision,
    required_operator_role text NOT NULL,
    policy_version text NOT NULL,
    decision text,
    operator_principal_id text,
    operator_session_id text
);

CREATE INDEX core_approval_pending_idx
    ON core_approval_requests (created_at) WHERE state = 'PENDING';

CREATE TABLE core_execution_records (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    approval_id text NOT NULL UNIQUE REFERENCES core_approval_requests(id),
    proposal_id text NOT NULL REFERENCES core_tool_proposals(id),
    action_digest text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    state text NOT NULL,
    attempt integer NOT NULL,
    reserved_at double precision NOT NULL
);

CREATE TABLE core_events (
    run_id text NOT NULL,
    revision bigint NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL,
    published_at double precision NOT NULL,
    PRIMARY KEY (run_id, revision)
);

CREATE TABLE core_checkpoints (
    run_id text PRIMARY KEY,
    revision bigint NOT NULL,
    state jsonb NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core_audit_records (
    run_id text NOT NULL,
    sequence bigint NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL,
    written_at double precision NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

CREATE TABLE core_a2a_tasks (
    task_id text NOT NULL,
    owner text NOT NULL,
    tenant text NOT NULL,
    context_id text NOT NULL,
    state integer NOT NULL,
    status_timestamp double precision,
    payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, owner, tenant)
);

CREATE INDEX core_a2a_tasks_list_idx
    ON core_a2a_tasks (owner, tenant, status_timestamp DESC, task_id DESC);

CREATE OR REPLACE FUNCTION core_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'row is immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_tool_proposals_immutable
BEFORE UPDATE OR DELETE ON core_tool_proposals
FOR EACH ROW EXECUTE FUNCTION core_reject_mutation();

CREATE TRIGGER core_audit_records_immutable
BEFORE UPDATE OR DELETE ON core_audit_records
FOR EACH ROW EXECUTE FUNCTION core_reject_mutation();
""",
    2: """
ALTER TABLE core_events
    ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE core_checkpoints
    ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE core_audit_records
    ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE core_execution_records
    ADD COLUMN started_at double precision,
    ADD COLUMN finished_at double precision,
    ADD COLUMN outcome jsonb,
    ADD COLUMN error_code text;
ALTER TABLE core_a2a_tasks
    ADD COLUMN protocol_version text NOT NULL DEFAULT '1.0',
    ADD COLUMN extension_version text NOT NULL DEFAULT 'v1';

CREATE INDEX core_events_tenant_run_idx
    ON core_events (tenant_id, run_id, revision);
CREATE INDEX core_checkpoints_tenant_run_idx
    ON core_checkpoints (tenant_id, run_id);
CREATE INDEX core_audit_tenant_run_idx
    ON core_audit_records (tenant_id, run_id, sequence);

CREATE TABLE core_runs (
    run_id text PRIMARY KEY,
    task_id text NOT NULL,
    context_id text NOT NULL,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    parent_run_id text,
    state text NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    request jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    pending_approval_id text,
    lease_owner text,
    lease_token text,
    lease_expires_at double precision,
    result jsonb,
    error_code text,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    UNIQUE (task_id, tenant_id, owner_id)
);

CREATE INDEX core_runs_recovery_idx
    ON core_runs (state, updated_at)
    WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REJECTED', 'ABORTED');

CREATE TABLE core_background_tasks (
    id text PRIMARY KEY,
    owner_run_id text NOT NULL,
    tenant_id text NOT NULL,
    kind text NOT NULL,
    state text NOT NULL,
    required boolean NOT NULL,
    recoverable boolean NOT NULL,
    revision bigint NOT NULL DEFAULT 0,
    contract jsonb NOT NULL,
    result jsonb,
    error_code text,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL
);

CREATE INDEX core_background_owner_idx
    ON core_background_tasks (tenant_id, owner_run_id, created_at);
CREATE INDEX core_background_recovery_idx
    ON core_background_tasks (state, updated_at)
    WHERE state IN ('submitted', 'working');

CREATE TABLE core_notifications (
    id text PRIMARY KEY,
    owner_run_id text NOT NULL,
    tenant_id text NOT NULL,
    task_id text NOT NULL,
    kind text NOT NULL,
    revision bigint NOT NULL,
    payload jsonb NOT NULL,
    acknowledged_at double precision,
    created_at double precision NOT NULL,
    UNIQUE (tenant_id, owner_run_id, task_id, kind, revision)
);

CREATE INDEX core_notifications_pending_idx
    ON core_notifications (tenant_id, owner_run_id, created_at)
    WHERE acknowledged_at IS NULL;

CREATE TABLE core_outbox (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at double precision NOT NULL,
    locked_by text,
    locked_until double precision,
    published_at double precision,
    last_error_code text,
    created_at double precision NOT NULL,
    UNIQUE (tenant_id, aggregate_type, aggregate_id, event_type, sequence)
);

CREATE INDEX core_outbox_pending_idx
    ON core_outbox (available_at, created_at)
    WHERE published_at IS NULL;
""",
    3: """
CREATE TABLE core_push_notification_configs (
    task_id text NOT NULL,
    config_id text NOT NULL,
    owner text NOT NULL,
    tenant_id text NOT NULL,
    encrypted_payload bytea NOT NULL,
    updated_at double precision NOT NULL,
    PRIMARY KEY (task_id, config_id, owner, tenant_id)
);

CREATE INDEX core_push_configs_dispatch_idx
    ON core_push_notification_configs (task_id, config_id);

CREATE TABLE core_push_deliveries (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    config_id text NOT NULL,
    event_key text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL,
    attempts integer NOT NULL,
    available_at double precision NOT NULL,
    locked_until double precision,
    last_error_code text,
    delivered_at double precision,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    UNIQUE (task_id, config_id, event_key)
);

CREATE INDEX core_push_deliveries_pending_idx
    ON core_push_deliveries (available_at, created_at)
    WHERE state != 'delivered';
""",
    4: """
CREATE TABLE core_artifacts (
    id text NOT NULL,
    tenant_id text NOT NULL,
    media_type text NOT NULL,
    digest text NOT NULL,
    size bigint NOT NULL,
    provenance jsonb NOT NULL,
    state text NOT NULL,
    created_at double precision NOT NULL,
    deleted_at double precision,
    PRIMARY KEY (id, tenant_id)
);

CREATE INDEX core_artifacts_digest_idx
    ON core_artifacts (digest) WHERE state = 'active';
CREATE INDEX core_artifacts_run_idx
    ON core_artifacts (tenant_id, ((provenance->>'run_id')))
    WHERE state = 'active';
""",
    5: """
UPDATE core_a2a_tasks SET tenant = 'default' WHERE tenant = '';
""",
    6: """
CREATE TABLE core_budget_ledgers (
    root_run_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    max_model_turns integer NOT NULL,
    max_tool_calls integer NOT NULL,
    used_model_turns integer NOT NULL DEFAULT 0,
    used_tool_calls integer NOT NULL DEFAULT 0,
    updated_at double precision NOT NULL
);
""",
    7: """
CREATE TABLE core_inbound_messages (
    run_id text NOT NULL REFERENCES core_runs(run_id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    message_id text NOT NULL,
    context_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    provenance jsonb NOT NULL,
    received_at double precision NOT NULL,
    consumed_at double precision,
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, message_id)
);

CREATE INDEX core_inbound_pending_idx
    ON core_inbound_messages (run_id, sequence)
    WHERE consumed_at IS NULL;
""",
    8: """
DROP TRIGGER IF EXISTS core_tool_proposals_immutable ON core_tool_proposals;
DROP TABLE IF EXISTS core_execution_records;
DROP TABLE IF EXISTS core_approval_requests;
DROP TABLE IF EXISTS core_tool_proposals;
""",
    9: """
CREATE TABLE core_memory_documents (
    app_name text NOT NULL,
    user_id text NOT NULL,
    memory_id text NOT NULL,
    namespace text NOT NULL,
    path text NOT NULL,
    content text NOT NULL,
    revision integer NOT NULL,
    embedding real[],
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, memory_id)
);

CREATE INDEX core_memory_namespace_idx
    ON core_memory_documents (app_name, user_id, namespace);

CREATE TABLE core_memory_document_versions (
    app_name text NOT NULL,
    user_id text NOT NULL,
    memory_id text NOT NULL,
    namespace text NOT NULL,
    path text NOT NULL,
    content text NOT NULL,
    revision integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, memory_id, revision)
);

CREATE TABLE core_memory_revisions (
    app_name text NOT NULL,
    user_id text NOT NULL,
    repository_revision integer NOT NULL,
    resolutions jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, repository_revision)
);
""",
    10: """
ALTER TABLE core_memory_documents ADD COLUMN entities jsonb;
""",
    11: """
ALTER TABLE core_background_tasks
    ADD COLUMN claim_owner text,
    ADD COLUMN claim_token text,
    ADD COLUMN claim_expires_at double precision,
    ADD COLUMN mutating boolean NOT NULL DEFAULT true;

DROP INDEX core_background_recovery_idx;
CREATE INDEX core_background_recovery_idx
    ON core_background_tasks (state, claim_expires_at, updated_at)
    WHERE state IN ('submitted', 'working');
""",
    12: """
ALTER TABLE core_runs
    ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
""",
    13: """
CREATE TABLE core_chats (
    tenant_id text NOT NULL,
    context_id text NOT NULL,
    owner_id text NOT NULL,
    latest_root_run_id text REFERENCES core_runs(run_id),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, context_id)
);
CREATE INDEX core_chats_owner_idx ON core_chats (tenant_id, owner_id, context_id);
CREATE TABLE core_root_messages (
    tenant_id text NOT NULL,
    actor_id text NOT NULL,
    message_id text NOT NULL,
    request_digest text NOT NULL,
    fingerprint_version integer NOT NULL DEFAULT 1 CHECK (fingerprint_version = 1),
    owner_id text NOT NULL,
    context_id text NOT NULL,
    task_id text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, actor_id, message_id),
    FOREIGN KEY (tenant_id, context_id) REFERENCES core_chats (tenant_id, context_id),
    FOREIGN KEY (task_id, owner_id, tenant_id) REFERENCES core_a2a_tasks (task_id, owner, tenant)
);
CREATE INDEX core_root_messages_task_idx ON core_root_messages (tenant_id, task_id);
""",
    14: """
CREATE TABLE core_waits (
    wait_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES core_runs(run_id) ON DELETE CASCADE,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    context_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    kind text NOT NULL CHECK (kind IN
        ('timer','task','tool_approval','owner_question','guardrail')),
    source_id text NOT NULL,
    subject jsonb NOT NULL,
    continuation jsonb NOT NULL,
    deadline double precision,
    outcome jsonb,
    resolved_at double precision,
    applied_at double precision,
    created_at double precision NOT NULL,
    UNIQUE (run_id, generation),
    CHECK ((outcome IS NULL) = (resolved_at IS NULL)),
    CHECK (applied_at IS NULL OR resolved_at IS NOT NULL)
);
CREATE UNIQUE INDEX core_waits_active_run_idx
    ON core_waits(run_id) WHERE applied_at IS NULL;
CREATE INDEX core_waits_due_idx ON core_waits(deadline)
    WHERE resolved_at IS NULL;
CREATE INDEX core_waits_pending_idx ON core_waits(kind, created_at, wait_id)
    WHERE resolved_at IS NULL;
""",
    15: """
CREATE TABLE core_owner_settings (
    tenant_id text PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    hitl_timeout_seconds integer NOT NULL DEFAULT 86400
        CHECK (hitl_timeout_seconds BETWEEN 1 AND 2147483647),
    owner_answer_timeout_seconds integer NOT NULL DEFAULT 86400
        CHECK (owner_answer_timeout_seconds BETWEEN 1 AND 2147483647),
    guardrails_timeout_seconds integer NOT NULL DEFAULT 86400
        CHECK (guardrails_timeout_seconds BETWEEN 1 AND 2147483647)
);
CREATE TABLE core_tool_policies (
    tenant_id text NOT NULL,
    canonical_name text NOT NULL,
    origin text NOT NULL,
    mode text NOT NULL DEFAULT 'require_hitl' CHECK (mode IN ('allow', 'require_hitl', 'deny')),
    guardrails_exempt boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    PRIMARY KEY (tenant_id, canonical_name, origin)
);
""",
    16: """
CREATE TABLE core_chat_file_batches (
    batch_id text PRIMARY KEY CHECK (batch_id ~ '^[0-9a-f]{32}$'),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    tenant_id text NOT NULL,
    actor_id text NOT NULL,
    message_id text NOT NULL,
    request_digest text NOT NULL,
    created_at double precision NOT NULL,
    storage_key text NOT NULL UNIQUE CHECK (storage_key = batch_id),
    lease_owner text NOT NULL,
    lease_token text NOT NULL,
    lease_expires_at double precision NOT NULL,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    state text NOT NULL DEFAULT 'staging' CHECK (state IN
        ('staging','accepted_quarantine','accepted_ready','published','excluded','rejected')),
    manifest jsonb,
    context_id text,
    owner_id text,
    task_id text,
    run_id text REFERENCES core_runs(run_id),
    sequence bigint CHECK (sequence > 0),
    decision_ref text,
    published_at double precision,
    error_code text,
    cleaned_at double precision,
    FOREIGN KEY (tenant_id, context_id) REFERENCES core_chats (tenant_id, context_id),
    FOREIGN KEY (task_id, owner_id, tenant_id) REFERENCES core_a2a_tasks (task_id, owner, tenant),
    CHECK (state IN ('staging','rejected') OR
        (context_id IS NOT NULL AND owner_id IS NOT NULL AND task_id IS NOT NULL
         AND run_id IS NOT NULL AND COALESCE(
             manifest->>'schema_version' = '1'
             AND jsonb_typeof(manifest->'entries') = 'array'
             AND jsonb_array_length(manifest->'entries') > 0, false))),
    CHECK (state NOT IN ('accepted_ready','published') OR decision_ref IS NOT NULL),
    CHECK ((state = 'published') = (published_at IS NOT NULL)),
    CHECK (cleaned_at IS NULL OR state = 'rejected')
);
CREATE INDEX core_chat_file_batches_scope_idx
    ON core_chat_file_batches (tenant_id, context_id, owner_id);
CREATE INDEX core_chat_file_batches_orphan_idx
    ON core_chat_file_batches (created_at, batch_id)
    WHERE state IN ('staging','rejected') AND cleaned_at IS NULL;
CREATE FUNCTION core_chat_file_batch_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.batch_id, NEW.schema_version, NEW.tenant_id, NEW.actor_id,
           NEW.message_id, NEW.request_digest, NEW.created_at, NEW.storage_key)
       IS DISTINCT FROM
       ROW(OLD.batch_id, OLD.schema_version, OLD.tenant_id, OLD.actor_id,
           OLD.message_id, OLD.request_digest, OLD.created_at, OLD.storage_key) THEN
        RAISE EXCEPTION 'immutable file batch identity';
    END IF;
    IF OLD.state NOT IN ('staging','rejected') AND
       ROW(NEW.manifest, NEW.context_id, NEW.owner_id, NEW.task_id, NEW.run_id, NEW.sequence)
       IS DISTINCT FROM
       ROW(OLD.manifest, OLD.context_id, OLD.owner_id, OLD.task_id, OLD.run_id, OLD.sequence) THEN
        RAISE EXCEPTION 'immutable accepted file batch';
    END IF;
    IF NOT (NEW.state = OLD.state OR
       (OLD.state = 'staging' AND NEW.state IN ('accepted_quarantine','rejected')) OR
       (OLD.state = 'accepted_quarantine' AND NEW.state IN ('accepted_ready','excluded')) OR
       (OLD.state = 'accepted_ready' AND NEW.state IN ('published','excluded'))) THEN
        RAISE EXCEPTION 'invalid file batch transition';
    END IF;
    IF OLD.decision_ref IS NOT NULL AND NEW.decision_ref IS DISTINCT FROM OLD.decision_ref THEN
        RAISE EXCEPTION 'immutable file batch decision';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER core_chat_file_batches_transition BEFORE UPDATE ON core_chat_file_batches
    FOR EACH ROW EXECUTE FUNCTION core_chat_file_batch_transition();
""",
    17: """
CREATE TABLE core_material_reviews (
    review_id text PRIMARY KEY,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    tenant_id text NOT NULL,
    run_id text NOT NULL REFERENCES core_runs(run_id),
    owner_id text NOT NULL,
    context_id text NOT NULL,
    source_id text NOT NULL CHECK (source_id <> ''),
    source_kind text NOT NULL CHECK (length(source_kind) BETWEEN 1 AND 64),
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    payload jsonb,
    sealed_ref jsonb,
    completed_result_ref jsonb,
    state text NOT NULL CHECK (state IN ('checking','clear','pending','allowed','rejected','timed_out')),
    deadline double precision NOT NULL CHECK (deadline > '-Infinity' AND deadline < 'Infinity'),
    max_calls bigint NOT NULL CHECK (max_calls > 0),
    max_input_tokens bigint NOT NULL CHECK (max_input_tokens > 0),
    attempts_used bigint NOT NULL CHECK (attempts_used BETWEEN 0 AND max_calls),
    input_tokens_used bigint NOT NULL CHECK (input_tokens_used BETWEEN 0 AND max_input_tokens),
    classification jsonb,
    wait_id text UNIQUE REFERENCES core_waits(wait_id),
    attempt_token text,
    revision bigint NOT NULL CHECK (revision > 0),
    created_at double precision NOT NULL,
    UNIQUE (tenant_id, run_id, source_id, content_digest),
    CHECK ((payload IS NULL) <> (sealed_ref IS NULL)),
    CHECK ((state IN ('pending','allowed','rejected','timed_out')) = (wait_id IS NOT NULL)),
    CHECK ((state = 'checking') = (classification IS NULL)),
    CHECK (attempts_used = 0 OR attempt_token IS NOT NULL)
);
CREATE FUNCTION core_material_review_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.review_id, NEW.schema_version, NEW.tenant_id, NEW.run_id, NEW.owner_id,
           NEW.context_id, NEW.source_id, NEW.source_kind, NEW.content_digest,
           NEW.payload, NEW.sealed_ref, NEW.completed_result_ref, NEW.deadline,
           NEW.max_calls, NEW.max_input_tokens, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.review_id, OLD.schema_version, OLD.tenant_id, OLD.run_id, OLD.owner_id,
           OLD.context_id, OLD.source_id, OLD.source_kind, OLD.content_digest,
           OLD.payload, OLD.sealed_ref, OLD.completed_result_ref, OLD.deadline,
           OLD.max_calls, OLD.max_input_tokens, OLD.created_at) THEN
        RAISE EXCEPTION 'immutable material review';
    END IF;
    IF NOT (NEW.state = OLD.state OR (OLD.state = 'checking' AND NEW.state IN ('clear','pending'))
            OR (OLD.state = 'pending' AND NEW.state IN ('allowed','rejected','timed_out')))
       OR NEW.attempts_used < OLD.attempts_used OR NEW.input_tokens_used < OLD.input_tokens_used
       OR NEW.revision <> OLD.revision + 1
       OR (OLD.attempt_token IS NOT NULL AND NEW.attempt_token IS DISTINCT FROM OLD.attempt_token)
       OR (OLD.wait_id IS NOT NULL AND NEW.wait_id IS DISTINCT FROM OLD.wait_id)
       OR (OLD.classification IS NOT NULL AND NEW.classification IS DISTINCT FROM OLD.classification) THEN
        RAISE EXCEPTION 'invalid material review transition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER core_material_reviews_transition BEFORE UPDATE ON core_material_reviews
    FOR EACH ROW EXECUTE FUNCTION core_material_review_transition();
""",
    18: """
ALTER TABLE core_owner_settings ADD COLUMN attachment_limit_bytes integer NOT NULL DEFAULT 25000000
    CHECK (attachment_limit_bytes BETWEEN 1 AND 2147483647);
""",
    19: """
ALTER TABLE core_owner_settings ADD COLUMN remote_timeout_seconds integer NOT NULL DEFAULT 86400
    CHECK (remote_timeout_seconds BETWEEN 1 AND 2147483647);
ALTER TABLE core_owner_settings ADD COLUMN remote_poll_interval_seconds integer NOT NULL DEFAULT 300
    CHECK (remote_poll_interval_seconds BETWEEN 1 AND 2147483647);

CREATE TABLE core_remote_agents (
    tenant_id text NOT NULL,
    id text NOT NULL,
    name text NOT NULL CHECK (name ~ '^[A-Za-z0-9_-]{1,128}$'),
    revision bigint NOT NULL CHECK (revision > 0),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name)
);
CREATE TABLE core_remote_agent_revisions (
    tenant_id text NOT NULL,
    id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    storage_version integer NOT NULL CHECK (storage_version = 1),
    url text NOT NULL,
    description text NOT NULL,
    enabled boolean NOT NULL,
    header_name text NOT NULL,
    encrypted_payload bytea,
    actor_id text NOT NULL,
    created_at double precision NOT NULL,
    PRIMARY KEY (tenant_id, id, revision),
    FOREIGN KEY (tenant_id, id) REFERENCES core_remote_agents (tenant_id, id)
);
ALTER TABLE core_remote_agents ADD CONSTRAINT core_remote_agent_current_revision
    FOREIGN KEY (tenant_id, id, revision)
    REFERENCES core_remote_agent_revisions (tenant_id, id, revision)
    DEFERRABLE INITIALLY DEFERRED;
""",
    20: """
ALTER TABLE core_background_tasks ADD COLUMN checkpoint jsonb;
CREATE INDEX core_background_remote_due_idx ON core_background_tasks
    (((checkpoint->>'next_poll_at')::double precision))
    WHERE kind = 'remote_a2a' AND state IN ('submitted', 'working');
""",
    21: """
ALTER TABLE core_chats ADD COLUMN workspace_revision bigint NOT NULL DEFAULT 0 CHECK (workspace_revision >= 0);
ALTER TABLE core_chats ADD CONSTRAINT core_chat_cleanup_scope UNIQUE (tenant_id, context_id, owner_id);
CREATE TABLE core_workspace_cleanups (
    tenant_id text NOT NULL,
    context_id text NOT NULL,
    owner_id text NOT NULL,
    request_id text NOT NULL,
    operation_id text NOT NULL UNIQUE,
    actor_id text NOT NULL,
    storage_version integer NOT NULL CHECK (storage_version = 1),
    request_digest text NOT NULL,
    selection jsonb NOT NULL CHECK (jsonb_typeof(selection) = 'array'),
    base_revision bigint NOT NULL CHECK (base_revision >= 0),
    workspace_revision bigint NOT NULL CHECK (workspace_revision >= base_revision),
    state text NOT NULL CHECK (state IN ('pending','completed','reconciliation')),
    results jsonb NOT NULL CHECK (jsonb_typeof(results) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, context_id, request_id),
    FOREIGN KEY (tenant_id, context_id, owner_id) REFERENCES core_chats (tenant_id, context_id, owner_id)
);
CREATE INDEX core_workspace_cleanup_pending ON core_workspace_cleanups (created_at, operation_id)
    WHERE state <> 'completed';
CREATE INDEX core_workspace_cleanup_latest ON core_workspace_cleanups (tenant_id,context_id,created_at DESC,operation_id DESC);
CREATE UNIQUE INDEX core_workspace_cleanup_one_pending ON core_workspace_cleanups (tenant_id, context_id)
    WHERE state <> 'completed';
CREATE FUNCTION core_workspace_cleanup_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF jsonb_array_length(NEW.results) <> jsonb_array_length(NEW.selection)
       OR (NEW.state <> 'completed' AND NEW.workspace_revision <> NEW.base_revision)
       OR (NEW.state = 'completed' AND (
           EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.results) r
                   WHERE r->>'status' IS NULL OR r->>'status' NOT IN ('deleted','skipped','error')
                      OR r->>'reason' = 'reconciliation_required')
           OR NEW.workspace_revision <> NEW.base_revision + CASE WHEN EXISTS
               (SELECT 1 FROM jsonb_array_elements(NEW.results) r WHERE r->>'status' = 'deleted')
               THEN 1 ELSE 0 END)) THEN
        RAISE EXCEPTION 'invalid workspace cleanup outcome';
    END IF;
    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    END IF;
    IF (NEW.tenant_id,NEW.context_id,NEW.owner_id,NEW.request_id,NEW.operation_id,NEW.actor_id,
        NEW.storage_version,NEW.request_digest,NEW.selection,NEW.base_revision,NEW.created_at)
       IS DISTINCT FROM
       (OLD.tenant_id,OLD.context_id,OLD.owner_id,OLD.request_id,OLD.operation_id,OLD.actor_id,
        OLD.storage_version,OLD.request_digest,OLD.selection,OLD.base_revision,OLD.created_at)
       OR (OLD.state = 'completed' AND NEW IS DISTINCT FROM OLD)
       OR NEW.workspace_revision < OLD.workspace_revision
       OR NEW.workspace_revision > NEW.base_revision + 1 THEN
        RAISE EXCEPTION 'invalid workspace cleanup transition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER core_workspace_cleanup_transition BEFORE INSERT OR UPDATE ON core_workspace_cleanups
    FOR EACH ROW EXECUTE FUNCTION core_workspace_cleanup_transition();
""",

    22: """
CREATE FUNCTION core_chat_immutable_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.tenant_id,NEW.context_id,NEW.owner_id,NEW.schema_version)
        IS DISTINCT FROM (OLD.tenant_id,OLD.context_id,OLD.owner_id,OLD.schema_version) THEN
        RAISE EXCEPTION 'immutable canonical chat binding';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER core_chat_immutable_binding BEFORE UPDATE ON core_chats
    FOR EACH ROW EXECUTE FUNCTION core_chat_immutable_binding();
CREATE TABLE core_cron_schedules (
    id text NOT NULL,
    tenant_id text NOT NULL,
    context_id text NOT NULL,
    owner_id text NOT NULL,
    storage_version integer NOT NULL CHECK (storage_version=1),
    revision bigint NOT NULL CHECK (revision>=1),
    prompt text NOT NULL,
    expression text NOT NULL,
    timezone text NOT NULL,
    enabled boolean NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    next_due_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY(tenant_id,id),
    UNIQUE(tenant_id,id,context_id,owner_id),
    FOREIGN KEY(tenant_id,context_id,owner_id) REFERENCES core_chats(tenant_id,context_id,owner_id),
    CHECK ((enabled AND NOT deleted AND next_due_at IS NOT NULL) OR
           (NOT enabled AND next_due_at IS NULL))
);
CREATE INDEX core_cron_due ON core_cron_schedules(tenant_id,id,next_due_at) WHERE enabled AND NOT deleted;
CREATE FUNCTION core_cron_schedule_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.tenant_id,NEW.context_id,NEW.owner_id,NEW.storage_version,NEW.created_at)
       IS DISTINCT FROM (OLD.id,OLD.tenant_id,OLD.context_id,OLD.owner_id,OLD.storage_version,OLD.created_at)
       OR (OLD.deleted AND NEW IS DISTINCT FROM OLD)
       OR NEW.revision < OLD.revision OR NEW.revision > OLD.revision+1
       OR ((NEW.prompt,NEW.expression,NEW.timezone,NEW.enabled,NEW.deleted)
           IS DISTINCT FROM (OLD.prompt,OLD.expression,OLD.timezone,OLD.enabled,OLD.deleted)
           AND NEW.revision <> OLD.revision+1) THEN
        RAISE EXCEPTION 'invalid cron schedule transition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER core_cron_schedule_transition BEFORE UPDATE ON core_cron_schedules
    FOR EACH ROW EXECUTE FUNCTION core_cron_schedule_transition();
CREATE TABLE core_cron_events (
    seq bigserial PRIMARY KEY,
    id text NOT NULL UNIQUE,
    tenant_id text NOT NULL,
    schedule_id text NOT NULL,
    context_id text NOT NULL,
    owner_id text NOT NULL,
    storage_version integer NOT NULL CHECK(storage_version=1),
    kind text NOT NULL CHECK(kind IN ('created','updated','deleted','started','skipped')),
    schedule_revision bigint NOT NULL CHECK(schedule_revision>=1),
    actor_id text NOT NULL,
    source text NOT NULL CHECK(source IN ('owner','tool','manual','automatic')),
    request_id text,
    request_digest text,
    task_id text,
    run_id text,
    due_at timestamptz,
    through timestamptz,
    reason text CHECK(reason IN ('context_busy','late','service_unavailable','workspace_cleanup_pending')),
    history_run_id text,
    history_position jsonb,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL,
    UNIQUE(tenant_id,request_id),
    FOREIGN KEY(tenant_id,schedule_id,context_id,owner_id)
        REFERENCES core_cron_schedules(tenant_id,id,context_id,owner_id),
    CHECK((request_id IS NULL)=(request_digest IS NULL)),
    CHECK((history_run_id IS NULL)=(history_position IS NULL)),
    CHECK(history_position IS NULL OR (jsonb_typeof(history_position)='array' AND jsonb_array_length(history_position)=4)),
    CHECK((kind='skipped' AND reason IS NOT NULL AND due_at IS NOT NULL AND task_id IS NULL AND run_id IS NULL)
           OR (kind<>'skipped' AND reason IS NULL AND history_run_id IS NULL AND history_position IS NULL))
);
CREATE INDEX core_cron_event_history ON core_cron_events(tenant_id,context_id,history_run_id,seq DESC) WHERE kind='skipped';
CREATE INDEX core_cron_event_schedule ON core_cron_events(tenant_id,schedule_id,seq DESC);
CREATE FUNCTION core_cron_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable cron event';
END;
$$;
CREATE TRIGGER core_cron_event_immutable BEFORE UPDATE OR DELETE ON core_cron_events
    FOR EACH ROW EXECUTE FUNCTION core_cron_event_immutable();
""",

    23: """
CREATE OR REPLACE FUNCTION core_chat_file_batch_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.batch_id, NEW.schema_version, NEW.tenant_id, NEW.actor_id,
           NEW.message_id, NEW.request_digest, NEW.created_at, NEW.storage_key)
       IS DISTINCT FROM
       ROW(OLD.batch_id, OLD.schema_version, OLD.tenant_id, OLD.actor_id,
           OLD.message_id, OLD.request_digest, OLD.created_at, OLD.storage_key) THEN
        RAISE EXCEPTION 'immutable file batch identity';
    END IF;
    IF OLD.state NOT IN ('staging','rejected') AND
       ROW(NEW.manifest, NEW.context_id, NEW.owner_id, NEW.task_id, NEW.run_id, NEW.sequence)
       IS DISTINCT FROM
       ROW(OLD.manifest, OLD.context_id, OLD.owner_id, OLD.task_id, OLD.run_id, OLD.sequence) THEN
        RAISE EXCEPTION 'immutable accepted file batch';
    END IF;
    IF NOT (NEW.state = OLD.state OR
       (OLD.state = 'staging' AND NEW.state IN ('accepted_quarantine','rejected')) OR
       (OLD.state = 'accepted_quarantine' AND NEW.state IN ('accepted_ready','excluded')) OR
       (OLD.state = 'accepted_ready' AND NEW.state IN ('published','excluded'))) THEN
        RAISE EXCEPTION 'invalid file batch transition';
    END IF;
    IF OLD.decision_ref IS NOT NULL AND NEW.decision_ref IS DISTINCT FROM OLD.decision_ref
       AND NOT (OLD.state = 'accepted_ready' AND NEW.state = 'excluded'
                AND NEW.decision_ref IS NOT NULL AND NEW.decision_ref <> '') THEN
        RAISE EXCEPTION 'immutable file batch decision';
    END IF;
    RETURN NEW;
END;
$$;
""",

    24: """
ALTER TABLE core_memory_documents
    ADD COLUMN tenant_id text NOT NULL DEFAULT '';
ALTER TABLE core_memory_document_versions
    ADD COLUMN tenant_id text NOT NULL DEFAULT '';
ALTER TABLE core_memory_revisions
    ADD COLUMN tenant_id text NOT NULL DEFAULT '';

ALTER TABLE core_memory_documents DROP CONSTRAINT core_memory_documents_pkey;
ALTER TABLE core_memory_documents
    ADD PRIMARY KEY (tenant_id, app_name, user_id, memory_id);
ALTER TABLE core_memory_document_versions DROP CONSTRAINT core_memory_document_versions_pkey;
ALTER TABLE core_memory_document_versions
    ADD PRIMARY KEY (tenant_id, app_name, user_id, memory_id, revision);
ALTER TABLE core_memory_revisions DROP CONSTRAINT core_memory_revisions_pkey;
ALTER TABLE core_memory_revisions
    ADD PRIMARY KEY (tenant_id, app_name, user_id, repository_revision);

DROP INDEX core_memory_namespace_idx;
CREATE INDEX core_memory_namespace_idx
    ON core_memory_documents (tenant_id, app_name, user_id, namespace);
""",

}


class PostgresDatabase:
    """One bounded PostgreSQL pool shared by all production state adapters."""

    def __init__(
        self,
        url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 10,
    ):
        if not url:
            raise CoreError(
                "DATABASE_URL_REQUIRED",
                "set SESSION_DATABASE_URL, SESSION_POSTGRES_HOST or DATABASE_URL",
            )
        if min_size < 0 or max_size < 1 or min_size > max_size or timeout <= 0:
            raise CoreError("CONFIG_INVALID", "invalid database pool configuration")
        self._closed = False
        self.pool = ConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
            name="core-agent",
            kwargs={"autocommit": True, "row_factory": dict_row},
            # Managed PostgreSQL drops idle connections without telling the client;
            # without this the first request after a pause fails on a dead socket.
            check=ConnectionPool.check_connection,
        )
        try:
            self.pool.open(wait=True, timeout=timeout)
            self.check()
        except Exception:
            self.pool.close()
            raise CoreError(
                "DATABASE_UNAVAILABLE",
                "cannot reach the configured PostgreSQL host",
            ) from None

    @classmethod
    def from_environment(cls, url=None):
        def integer(name, default):
            try:
                return int(os.getenv(name, default))
            except ValueError:
                raise CoreError(
                    "CONFIG_INVALID", f"{name} must be an integer"
                ) from None

        try:
            timeout = float(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "10"))
        except ValueError:
            raise CoreError(
                "CONFIG_INVALID", "DATABASE_CONNECT_TIMEOUT_SECONDS must be numeric"
            ) from None
        return cls(
            os.getenv("DATABASE_URL", "") if url is None else url,
            min_size=integer("DATABASE_POOL_MIN", "1"),
            max_size=integer("DATABASE_POOL_MAX", "10"),
            timeout=timeout,
        )

    @contextmanager
    def transaction(self):
        with self.pool.connection() as connection:
            with connection.transaction():
                yield connection

    def check(self):
        with self.pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def schema_version(self):
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT to_regclass('core_schema_migrations') AS name"
            ).fetchone()
            if not row["name"]:
                return 0
            row = connection.execute(
                "SELECT COALESCE(max(version), 0) AS version FROM core_schema_migrations"
            ).fetchone()
            return row["version"]

    def migrate(self):
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(913728451)")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS core_schema_migrations (
                    version integer PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM core_schema_migrations"
                ).fetchall()
            }
            unknown = applied - MIGRATIONS.keys()
            if unknown:
                raise CoreError(
                    "DATABASE_SCHEMA_UNSUPPORTED",
                    "the database carries migrations this build does not know",
                )
            for version, sql in MIGRATIONS.items():
                if version not in applied:
                    connection.execute(sql)
                    connection.execute(
                        "INSERT INTO core_schema_migrations (version) VALUES (%s)",
                        (version,),
                    )
        self.verify_schema()

    def verify_schema(self):
        try:
            version = self.schema_version()
        except Exception:
            raise CoreError(
                "DATABASE_SCHEMA_UNAVAILABLE",
                "run the migration job before starting the agent",
            ) from None
        if version != SCHEMA_VERSION:
            raise CoreError(
                "DATABASE_SCHEMA_MISMATCH",
                f"expected schema {SCHEMA_VERSION}, got {version}",
            )

    def grant_application_role(self, role):
        if not role:
            raise CoreError("CONFIG_INVALID", "DATABASE_APP_ROLE is required")
        grants = {
            "core_schema_migrations": "SELECT",
            "core_events": "SELECT, INSERT, DELETE",
            "core_checkpoints": "SELECT, INSERT, UPDATE, DELETE",
            "core_audit_records": "SELECT, INSERT",
            "core_a2a_tasks": "SELECT, INSERT, UPDATE, DELETE",
            "core_runs": "SELECT, INSERT, UPDATE, DELETE",
            "core_waits": "SELECT, INSERT, UPDATE, DELETE",
            "core_owner_settings": "SELECT, INSERT, UPDATE",
            "core_remote_agents": "SELECT, INSERT, UPDATE",
            "core_remote_agent_revisions": "SELECT, INSERT",
            "core_tool_policies": "SELECT, INSERT, UPDATE",
            "core_chats": "SELECT, INSERT, UPDATE",
            "core_workspace_cleanups": "SELECT, INSERT, UPDATE",
            "core_cron_schedules": "SELECT, INSERT, UPDATE",
            "core_cron_events": "SELECT, INSERT",
            "core_root_messages": "SELECT, INSERT",
            "core_chat_file_batches": "SELECT, INSERT, UPDATE",
            "core_material_reviews": "SELECT, INSERT, UPDATE",
            "core_background_tasks": "SELECT, INSERT, UPDATE, DELETE",
            "core_notifications": "SELECT, INSERT, UPDATE, DELETE",
            "core_outbox": "SELECT, INSERT, UPDATE, DELETE",
            "core_push_notification_configs": "SELECT, INSERT, UPDATE, DELETE",
            "core_push_deliveries": "SELECT, INSERT, UPDATE",
            "core_artifacts": "SELECT, INSERT, UPDATE, DELETE",
            "core_budget_ledgers": "SELECT, INSERT, UPDATE, DELETE",
            "core_inbound_messages": "SELECT, INSERT, UPDATE, DELETE",
            "core_memory_documents": "SELECT, INSERT, UPDATE, DELETE",
            "core_memory_document_versions": "SELECT, INSERT, DELETE",
            "core_memory_revisions": "SELECT, INSERT, DELETE",
        }
        with self.transaction() as connection:
            connection.execute(
                SQL("GRANT USAGE ON SCHEMA public TO {}").format(Identifier(role))
            )
            connection.execute(SQL("GRANT USAGE ON SEQUENCE core_cron_events_seq_seq TO {}").format(Identifier(role)))
            for table, privileges in grants.items():
                connection.execute(
                    SQL("GRANT {} ON {} TO {}").format(
                        SQL(privileges), Identifier(table), Identifier(role)
                    )
                )

    def close(self):
        if not self._closed:
            self.pool.close()
            self._closed = True


class PostgresEventStore:
    def __init__(self, database):
        self.database = database

    def append(self, run_id, kind, data, *, tenant_id="default"):
        published_at = time.time()
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            row = connection.execute(
                """SELECT COALESCE(max(revision), 0) + 1 AS revision
                   FROM core_events WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()
            revision = row["revision"]
            connection.execute(
                """INSERT INTO core_events
                   (run_id, revision, kind, data, published_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (run_id, revision, kind, Jsonb(dict(data)), published_at, tenant_id),
            )
        return Event(run_id, revision, kind, dict(data), published_at)

    def events(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT run_id, revision, kind, data, published_at
                   FROM core_events WHERE run_id = %s AND tenant_id = %s
                   ORDER BY revision""",
                (run_id, tenant_id),
            ).fetchall()
        return tuple(Event(**row) for row in rows)

    def revision(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            return connection.execute(
                """SELECT COALESCE(max(revision), 0) AS revision
                   FROM core_events WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()["revision"]

    def count(self, run_id, *, kind=None, tenant_id="default"):
        sql = "SELECT count(*) AS count FROM core_events WHERE run_id = %s AND tenant_id = %s"
        values = [run_id, tenant_id]
        if kind is not None:
            sql += " AND kind = %s"
            values.append(kind)
        with self.database.pool.connection() as connection:
            return connection.execute(sql, values).fetchone()["count"]


class PostgresCheckpointStore:
    def __init__(self, database):
        self.database = database

    def save(self, run_id, revision, state, *, tenant_id="default"):
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_checkpoints (run_id, revision, state, tenant_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (run_id) DO UPDATE SET
                     revision = EXCLUDED.revision,
                     state = EXCLUDED.state,
                     tenant_id = EXCLUDED.tenant_id,
                     saved_at = now()
                   WHERE core_checkpoints.revision <= EXCLUDED.revision
                     AND core_checkpoints.tenant_id = EXCLUDED.tenant_id""",
                (run_id, revision, Jsonb(dict(state)), tenant_id),
            )

    def load(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT revision, state FROM core_checkpoints
                   WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()
        return (row["revision"], row["state"]) if row else None


class PostgresAuditLog:
    def __init__(self, database):
        self.database = database

    def append(self, run_id, kind, data, *, tenant_id="default"):
        written_at = time.time()
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            sequence = connection.execute(
                """SELECT COALESCE(max(sequence), 0) + 1 AS sequence
                   FROM core_audit_records WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()["sequence"]
            connection.execute(
                """INSERT INTO core_audit_records
                   (run_id, sequence, kind, data, written_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (run_id, sequence, kind, Jsonb(dict(data)), written_at, tenant_id),
            )
        return AuditRecord(run_id, sequence, kind, dict(data), written_at)

    def records(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT run_id, sequence, kind, data, written_at
                   FROM core_audit_records
                   WHERE run_id = %s AND tenant_id = %s ORDER BY sequence""",
                (run_id, tenant_id),
            ).fetchall()
        return tuple(AuditRecord(**row) for row in rows)

    def replace(self, run_id, sequence, data):
        raise CoreError("AUDIT_IMMUTABLE")


REMOTE_PROGRESS_KEY = "core_agent_remote_progress"


def reconcile_remote_progress(task, *, state, tasks, previous=None):
    """Project only committed safe enums; unchanged observations retain their displayed revision."""
    prior = MessageToDict((previous if previous is not None else task).metadata).get(REMOTE_PROGRESS_KEY, [])
    prior = {item["task_id"]: item for item in prior if isinstance(item, dict)
             and item.keys() == {"task_id", "revision", "agent_name", "remote_state"}
             and isinstance(item["task_id"], str)} if isinstance(prior, list) else {}
    entries = []
    if state not in {"COMPLETED", "FAILED", "ABORTED", "CANCELLED", "REJECTED"}:
        for row in tasks:
            result = row["result"]
            if (not isinstance(result, dict) or result.keys() != {"agent_name", "remote_state"}
                    or not isinstance(result["agent_name"], str)
                    or not isinstance(result["remote_state"], str) or result["remote_state"] not in REMOTE_PROGRESS_STATES):
                continue
            entry = {"task_id": row["id"], "revision": row["revision"], **result}
            old = prior.get(row["id"])
            if (old and old["agent_name"] == result["agent_name"] and old["remote_state"] == result["remote_state"]
                    and type(old["revision"]) in (int, float) and 0 < old["revision"] <= row["revision"]
                    and old["revision"] == int(old["revision"])):
                entry["revision"] = old["revision"]
            entries.append(entry)
    entries.sort(key=lambda item: item["task_id"])
    current = MessageToDict(task.metadata).get(REMOTE_PROGRESS_KEY)
    if entries == current or not entries and current is None:
        return False
    if entries:
        task.metadata[REMOTE_PROGRESS_KEY] = entries
        task.status.state = a2a_pb2.TASK_STATE_WORKING
    elif REMOTE_PROGRESS_KEY in task.metadata:
        del task.metadata[REMOTE_PROGRESS_KEY]
    task.status.timestamp.GetCurrentTime()
    return True


def _has_response_files(result):
    refs = (result or {}).get("outgoing_files", ())
    if not isinstance(refs, (tuple, list)):
        raise CoreError("ARTIFACT_INTEGRITY_FAILED")
    return bool(refs)


def _project_result_artifact(task, artifact, *, replace_all=False):
    if replace_all:
        del task.artifacts[:]
    projected = next((item for item in task.artifacts if item.artifact_id == artifact.id), None)
    if projected is None:
        projected = task.artifacts.add(artifact_id=artifact.id)
    else:
        del projected.parts[:]
        projected.metadata.Clear()
    for part in artifact.parts:
        if part.kind == "file":
            projected.parts.add(raw=part.data["bytes"], filename=part.data["filename"], media_type=part.data["media_type"])
        else:
            projected.parts.add(text=part.data, media_type="text/plain")
    projected.metadata.update({"digest": artifact.digest, "size": artifact.size,
        "recovered": True, "provenance": artifact.provenance})


def reconcile_workflow_task(task, *, run_id, state, result=None, error_code=None, version=None, authoritative=False,
                            record=None, response_files_service=None, connection=None):
    """Project canonical waiting/terminal state without exposing private waits."""
    terminal = {
        "COMPLETED": a2a_pb2.TASK_STATE_COMPLETED,
        "FAILED": a2a_pb2.TASK_STATE_FAILED,
        "ABORTED": a2a_pb2.TASK_STATE_FAILED,
        "CANCELLED": a2a_pb2.TASK_STATE_CANCELED,
        "REJECTED": a2a_pb2.TASK_STATE_REJECTED,
    }
    states = {**terminal, **{name: a2a_pb2.TASK_STATE_WORKING for name in (
        "RUNNING", "MODEL_RESPONDED", "EXECUTING", "WAITING_TASK", "WAITING_INPUT",
    )}}
    if state == "RUNNING" and version == 1:
        states[state] = a2a_pb2.TASK_STATE_SUBMITTED
    result = result or {}
    artifact = None
    if state == "COMPLETED" and _has_response_files(result):
        scope = record if isinstance(record, dict) else vars(record) if record is not None else {}
        if (scope.get("task_id"), scope.get("context_id")) != (task.id, task.context_id):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        artifact = workflow_result_artifact(record, response_files_service, connection=connection)
    if state not in states:
        return False
    if not authoritative and task.status.state in terminal.values():
        if artifact is not None and task.status.state == a2a_pb2.TASK_STATE_COMPLETED:
            _project_result_artifact(task, artifact, replace_all=True)
            return True
        return False
    prior_version = dict(task.metadata).get("core_agent_workflow_version", 0)
    if artifact is None and not authoritative and version is not None and isinstance(prior_version, (int, float)) and version <= prior_version:
        return False
    if version is None and state not in terminal and task.status.state == states[state]:
        return False
    task.status.state = states[state]
    task.status.timestamp.GetCurrentTime()
    task.status.ClearField("message")
    if version is not None:
        task.metadata["core_agent_workflow_version"] = version
    if state not in terminal:
        return True
    if state in {"FAILED", "ABORTED", "REJECTED"}:
        reason = (
            error_code
            or {
                "FAILED": "TASK_FAILED",
                "ABORTED": "SIDE_EFFECT_UNKNOWN",
                "REJECTED": "POLICY_DENIED",
            }[state]
        )
        task.status.message.Clear()
        task.status.message.message_id = (
            f"{task.id}:{state.lower()}"
        )
        task.status.message.task_id = task.id
        task.status.message.context_id = task.context_id
        task.status.message.role = a2a_pb2.ROLE_AGENT
        task.status.message.parts.add(
            text=reason,
            media_type="text/plain",
        )
    message = result.get("message")
    if authoritative and state == "COMPLETED":
        # In-flight SDK chunks are not an additional canonical final artifact.
        del task.artifacts[:]
    if state == "COMPLETED" and (message or artifact is not None):
        if artifact is None:
            artifact = workflow_result_artifact({"state": state, "run_id": run_id, "task_id": task.id, "result": result})
        _project_result_artifact(task, artifact, replace_all=bool(result.get("outgoing_files")))
    return True


class PostgresTaskStore(TaskStore):
    """Durable A2A task store scoped by authenticated owner and tenant."""

    def __init__(self, database, owner_resolver=resolve_user_scope):
        self.database = database
        self.owner_resolver = owner_resolver
        self.enqueue_notification = None
        self.response_files_service = getattr(database, "response_files_service", None)

    @staticmethod
    def _remote_rows(connection, run, tenant):
        if not run.get("snapshot", {}).get("remote_calls"):
            return ()
        return connection.execute("""SELECT id, revision, result FROM core_background_tasks
            WHERE tenant_id = %s AND owner_run_id = %s AND kind = 'remote_a2a'
              AND state = 'working'
              AND (checkpoint->>'deadline' IS NULL
                   OR (checkpoint->>'deadline')::double precision > extract(epoch FROM clock_timestamp()))
            ORDER BY id FOR SHARE""", (tenant, run["run_id"])).fetchall()

    def _write_projection(self, connection, task, *, owner, tenant, enqueue_notification=None):
        connection.execute("""UPDATE core_a2a_tasks SET state = %s, status_timestamp = %s,
            payload = %s, updated_at = now() WHERE task_id = %s AND owner = %s AND tenant = %s""",
            (int(task.status.state), task.status.timestamp.ToMilliseconds() / 1000,
             task.SerializeToString(), task.id, owner, tenant))
        notify = enqueue_notification or self.enqueue_notification
        if notify is not None:
            notify(task.id, task, owner=owner, tenant=tenant, connection=connection)

    def _project_progress(self, connection, row):
        """Read projection preserves the established explicit terminal-reconciliation boundary."""
        task = a2a_pb2.Task.FromString(bytes(row["payload"]))
        if task.status.state in (a2a_pb2.TASK_STATE_FAILED, a2a_pb2.TASK_STATE_CANCELED, a2a_pb2.TASK_STATE_REJECTED):
            return task
        run = connection.execute("""SELECT run_id, task_id, tenant_id, owner_id, context_id,
            state, version, result, error_code, snapshot FROM core_runs
            WHERE task_id = %s AND tenant_id = %s AND owner_id = %s FOR SHARE""",
            (task.id, row["tenant"], row["owner"])).fetchone()
        if run is None:
            return task
        if run["state"] == "COMPLETED" and _has_response_files(run["result"]):
            reconcile_workflow_task(task, run_id=run["run_id"], state=run["state"], result=run["result"],
                version=run["version"], record=run, response_files_service=self.response_files_service, connection=connection)
            return task
        if task.status.state == a2a_pb2.TASK_STATE_COMPLETED:
            return task
        if run["state"] in {"COMPLETED", "FAILED", "ABORTED", "CANCELLED", "REJECTED"}:
            # Do not enqueue a new working frame while terminal reconciliation is pending.
            if REMOTE_PROGRESS_KEY in task.metadata:
                del task.metadata[REMOTE_PROGRESS_KEY]
            return task
        if reconcile_remote_progress(task, state=run["state"], tasks=self._remote_rows(connection, run, row["tenant"])):
            self._write_projection(connection, task, owner=row["owner"], tenant=row["tenant"])
        return task

    def _scope(self, context):
        owner = self.owner_resolver(context)
        return (
            owner if context.user.is_authenticated and owner else "anonymous"
        ), context.tenant or "default"

    def reconcile_from_workflows(self, *, enqueue_notification=None):
        reconciled = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT task.payload, task.owner, task.tenant, run.run_id,
                          run.task_id
                   FROM core_a2a_tasks task
                   JOIN core_runs run ON run.task_id = task.task_id
                    AND run.owner_id = task.owner
                    AND run.tenant_id = task.tenant
                   WHERE task.state NOT IN (%s, %s, %s, %s)
                     AND (run.state IN ('COMPLETED','FAILED','ABORTED','CANCELLED','REJECTED',
                                        'WAITING_TASK','WAITING_INPUT')
                          OR run.state IN ('RUNNING','MODEL_RESPONDED','EXECUTING') AND run.snapshot ? 'remote_calls')
                   FOR UPDATE OF task""",
                (
                    int(a2a_pb2.TASK_STATE_COMPLETED),
                    int(a2a_pb2.TASK_STATE_FAILED),
                    int(a2a_pb2.TASK_STATE_CANCELED),
                    int(a2a_pb2.TASK_STATE_REJECTED),
                ),
            ).fetchall()
            for row in rows:
                # The task lock may have waited; reread canonical state afterwards.
                run = connection.execute(
                    "SELECT run_id, task_id, tenant_id, owner_id, context_id, state, version, result, error_code, snapshot FROM core_runs WHERE run_id = %s FOR SHARE",
                    (row["run_id"],),
                ).fetchone()
                task = a2a_pb2.Task.FromString(bytes(row["payload"]))
                changed = reconcile_workflow_task(
                    task, run_id=row["run_id"], state=run["state"], version=run["version"],
                    result=run["result"], error_code=run["error_code"],
                    record=run, response_files_service=self.response_files_service, connection=connection,
                )
                changed = reconcile_remote_progress(task, state=run["state"],
                    tasks=self._remote_rows(connection, run, row["tenant"])) or changed
                if not changed:
                    continue
                self._write_projection(connection, task, owner=row["owner"], tenant=row["tenant"], enqueue_notification=enqueue_notification)
                reconciled += 1
        return reconciled

    def _save(self, task, context, *, connection=None):
        # The SDK keeps its aggregate and may append further Artifact chunks.
        persisted = a2a_pb2.Task()
        persisted.CopyFrom(task)
        task = persisted
        owner, tenant = self._scope(context)
        timestamp = None
        if task.HasField("status") and task.status.HasField("timestamp"):
            timestamp = task.status.timestamp.ToMilliseconds() / 1000
        with (self.database.transaction() if connection is None else nullcontext(connection)) as connection:
            if is_company_owner(context):
                existing = connection.execute(
                    "SELECT owner FROM core_a2a_tasks WHERE task_id = %s AND tenant = %s FOR UPDATE",
                    (task.id, tenant),
                ).fetchall()
                if len(existing) > 1:
                    raise InvalidParamsError("Task not found")
                if existing:
                    owner = existing[0]["owner"]
            previous = connection.execute(
                "SELECT payload FROM core_a2a_tasks WHERE task_id = %s AND owner = %s AND tenant = %s FOR UPDATE",
                (task.id, owner, tenant),
            ).fetchone()
            if previous:
                stored = a2a_pb2.Task.FromString(bytes(previous["payload"]))
                stored_terminal = stored.status.state in (
                    a2a_pb2.TASK_STATE_COMPLETED, a2a_pb2.TASK_STATE_FAILED,
                    a2a_pb2.TASK_STATE_CANCELED, a2a_pb2.TASK_STATE_REJECTED,
                )
                run = connection.execute(
                    """SELECT run_id, task_id, tenant_id, owner_id, context_id, state, version, result, error_code, snapshot FROM core_runs
                       WHERE task_id = %s AND owner_id = %s AND tenant_id = %s FOR SHARE""",
                    (task.id, owner, tenant),
                ).fetchone()
                incoming_version = dict(task.metadata).get("core_agent_workflow_version", 0)
                if run and (stored_terminal or run["state"] == "COMPLETED" and _has_response_files(run["result"])
                            or not isinstance(incoming_version, (int, float)) or incoming_version < run["version"]):
                    # SDK events carry an older Task snapshot; only its history may
                    # advance independently of the canonical workflow revision.
                    reconcile_workflow_task(
                        stored if stored_terminal else task, run_id=run["run_id"], state=run["state"],
                        result=run["result"], error_code=run["error_code"],
                        version=run["version"], authoritative=not stored_terminal,
                        record=run, response_files_service=self.response_files_service, connection=connection,
                    )
                    timestamp = task.status.timestamp.ToMilliseconds() / 1000
                if stored_terminal:
                    return
                if run:
                    reconcile_remote_progress(task, state=run["state"], tasks=self._remote_rows(connection, run, tenant), previous=stored)
                    timestamp = task.status.timestamp.ToMilliseconds() / 1000
            connection.execute(
                """INSERT INTO core_a2a_tasks
                   (task_id, owner, tenant, context_id, state, status_timestamp, payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (task_id, owner, tenant) DO UPDATE SET
                     context_id = EXCLUDED.context_id,
                     state = EXCLUDED.state,
                     status_timestamp = EXCLUDED.status_timestamp,
                     payload = EXCLUDED.payload,
                     updated_at = now()
                   WHERE core_a2a_tasks.state NOT IN (%s, %s, %s, %s)""",
                (
                    task.id,
                    owner,
                    tenant,
                    task.context_id,
                    int(task.status.state),
                    timestamp,
                    task.SerializeToString(),
                    int(a2a_pb2.TASK_STATE_COMPLETED),
                    int(a2a_pb2.TASK_STATE_FAILED),
                    int(a2a_pb2.TASK_STATE_CANCELED),
                    int(a2a_pb2.TASK_STATE_REJECTED),
                ),
            )
            if previous and run and self.enqueue_notification is not None:
                before = MessageToDict(stored.metadata).get(REMOTE_PROGRESS_KEY)
                after = MessageToDict(task.metadata).get(REMOTE_PROGRESS_KEY)
                if before != after and run["state"] not in {"COMPLETED", "FAILED", "ABORTED", "CANCELLED", "REJECTED"}:
                    self.enqueue_notification(task.id, task, owner=owner, tenant=tenant, connection=connection)

    def _get(self, task_id, context):
        owner, tenant = self._scope(context)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT owner, tenant, payload FROM core_a2a_tasks WHERE task_id = %s AND (owner = %s OR %s) AND tenant = %s FOR UPDATE",
                (task_id, owner, is_company_owner(context), tenant),
            ).fetchall()
            row = rows[0] if len(rows) == 1 else None
            task = self._project_progress(connection, row) if row else None
        if row and is_company_owner(context):
            context.user = ScopeUser(row["owner"])
        return task

    def _list(self, params, context):
        owner, tenant = self._scope(context)
        sql = "SELECT owner, tenant, payload FROM core_a2a_tasks WHERE (owner = %s OR %s) AND tenant = %s"
        values = [owner, is_company_owner(context), tenant]
        if params.context_id:
            sql += " AND context_id = %s"
            values.append(params.context_id)
        if params.status:
            sql += " AND state = %s"
            values.append(int(params.status))
        if params.HasField("status_timestamp_after"):
            sql += " AND status_timestamp >= %s"
            values.append(params.status_timestamp_after.ToMilliseconds() / 1000)
        sql += " ORDER BY status_timestamp DESC NULLS LAST, task_id DESC"
        with self.database.transaction() as connection:
            rows = connection.execute(sql + " FOR UPDATE", values).fetchall()
            tasks = [self._project_progress(connection, row) for row in rows]
        total_size = len(tasks)
        start_idx = 0
        if params.page_token:
            task_id = decode_page_token(params.page_token)
            for index, task in enumerate(tasks):
                if task.id == task_id:
                    start_idx = index
                    break
            else:
                raise InvalidParamsError(f"Invalid page token: {params.page_token}")
        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        end_idx = start_idx + page_size
        next_token = (
            encode_page_token(tasks[end_idx].id) if end_idx < total_size else None
        )
        return a2a_pb2.ListTasksResponse(
            next_page_token=next_token,
            tasks=tasks[start_idx:end_idx],
            total_size=total_size,
            page_size=page_size,
        )

    def _delete(self, task_id, context):
        owner, tenant = self._scope(context)
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM core_a2a_tasks WHERE task_id = %s AND (owner = %s OR %s) AND tenant = %s",
                (task_id, owner, is_company_owner(context), tenant),
            )

    async def save(self, task, context):
        await asyncio.to_thread(self._save, task, context)

    async def get(self, task_id, context):
        return await asyncio.to_thread(self._get, task_id, context)

    async def list(self, params, context):
        return await asyncio.to_thread(self._list, params, context)

    async def delete(self, task_id, context):
        await asyncio.to_thread(self._delete, task_id, context)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage core-agent PostgreSQL schema and explicit remote import")
    parser.add_argument(
        "command", nargs="?", default="migrate", choices=("migrate", "check", "import-remote-agents")
    )
    parser.add_argument("--file", help="Protected version1 remote import JSON file")
    args = parser.parse_args(argv)
    if (args.command == "import-remote-agents") != (args.file is not None):
        parser.error("--file is required only for import-remote-agents")
    if args.command == "import-remote-agents":
        from .remote_registry import PostgresRemoteRegistry, read_legacy_peer_import

        tenant = os.getenv("CORE_AGENT_TENANT_ID", "")
        if not tenant.strip():
            raise CoreError("CONFIG_INVALID", "CORE_AGENT_TENANT_ID is required for operator import")
        entries = read_legacy_peer_import(args.file)
    database = PostgresDatabase.from_environment(
        os.getenv("DATABASE_MIGRATION_URL") or os.getenv("DATABASE_URL", "")
    )
    try:
        if args.command == "migrate":
            database.migrate()
            if os.getenv("DATABASE_APP_ROLE"):
                database.grant_application_role(os.environ["DATABASE_APP_ROLE"])
        else:
            database.verify_schema()
            if args.command == "import-remote-agents":
                registry = PostgresRemoteRegistry(database, os.getenv("PUSH_NOTIFICATION_ENCRYPTION_KEY"))
                imported = registry.import_legacy(tenant, entries)
                print(f"Imported {len(imported)} remote agents")
    except PostgresError:
        if args.command == "import-remote-agents":
            raise CoreError("REMOTE_IMPORT_FAILED") from None
        raise
    finally:
        database.close()


if __name__ == "__main__":
    main()
