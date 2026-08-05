---
name: sql-queries
description: Design, explain, review, translate, or optimize read-only analytical SQL across common database and warehouse dialects with explicit grain and validation.
license: Apache-2.0
modified: Adapted for Core Agent; differs from the pinned upstream version.
---

# Read-only SQL queries

1. Establish the dialect, schema, table grain, keys, metric definitions,
   timezone, and expected result grain. Inspect metadata with an enabled database
   capability instead of guessing names or types.
2. Produce only read-only statements: `SELECT`, read-only CTEs, `VALUES`, or plain
   `EXPLAIN` when supported. Never issue DDL, DML, data-modifying CTEs, `CALL`,
   `COPY`, export/unload, maintenance, session changes, temporary objects, or
   `EXPLAIN ANALYZE`.
3. Parameterize external values. Use explicit columns, deterministic ordering,
   qualified names, clear aliases, documented null handling, and a bounded
   `LIMIT` for exploration.
4. Validate join cardinality, duplicate amplification, denominators, date
   boundaries, timezones, units, and the effect of filters. Add small read-only
   reconciliation queries for critical totals.
5. Optimize only after correctness: project needed columns, filter early, use
   partition or clustering predicates, avoid accidental cross joins, and inspect
   a plain plan where available.
6. Return the SQL with dialect and schema assumptions, expected row grain,
   validation steps, and any unverified dependency.

The skill is guidance, not an authorization boundary. Execute SQL only through an
enabled tool and a database principal independently enforced as read-only. Never
claim a query ran unless its tool result proves it.
