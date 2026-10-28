-- Run with psql -X; required variables: tenant_id, app_name, user_id, namespace.
-- Inventory only: no content, paths, document IDs or embedding values are returned.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '15s';
SET LOCAL lock_timeout = '2s';

WITH scope AS (
    SELECT :'tenant_id'::text AS tenant_id, :'app_name'::text AS app_name,
           :'user_id'::text AS user_id, :'namespace'::text AS namespace
), dimensions AS (
    SELECT d.embedding IS NOT NULL AS embedding_present,
           array_ndims(d.embedding) AS array_dimensions,
           array_length(d.embedding, 1) AS vector_dimensions, count(*) AS documents
    FROM core_memory_documents d CROSS JOIN scope s
    WHERE d.tenant_id = s.tenant_id AND d.app_name = s.app_name
      AND d.user_id = s.user_id AND d.namespace = s.namespace
    GROUP BY 1, 2, 3
), indexes AS (
    SELECT c.relname AS name, i.indisvalid AS valid, i.indisready AS ready,
           pg_get_indexdef(i.indexrelid) AS definition
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    WHERE i.indrelid = 'core_memory_documents'::regclass
)
SELECT jsonb_build_object(
    'observed_at', transaction_timestamp(),
    'server_version', current_setting('server_version'),
    'database', current_database(), 'database_role', current_user,
    'memory_table', (SELECT format('%I.%I', n.nspname, c.relname)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = 'core_memory_documents'::regclass),
    'read_only', current_setting('transaction_read_only'),
    'isolation', current_setting('transaction_isolation'),
    'vector_extension_version', (SELECT extversion FROM pg_extension WHERE extname = 'vector'),
    'scope', (SELECT to_jsonb(s) FROM scope s),
    'repository_revision', (
        SELECT max(r.repository_revision) FROM core_memory_revisions r CROSS JOIN scope s
        WHERE r.tenant_id = s.tenant_id AND r.app_name = s.app_name AND r.user_id = s.user_id
    ),
    'dimensions', COALESCE((SELECT jsonb_agg(to_jsonb(d)
        ORDER BY embedding_present, array_dimensions, vector_dimensions) FROM dimensions d), '[]'::jsonb),
    'indexes', COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY name) FROM indexes i), '[]'::jsonb)
);
ROLLBACK;
