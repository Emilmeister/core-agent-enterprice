---
name: explore-data
description: Profile CSV, Excel, JSON, Parquet, SQL results, or other tables to reveal structure, quality, distributions, trends, relationships, and anomalies before deeper analysis.
license: Apache-2.0
modified: Adapted for Core Agent; differs from the pinned upstream version.
---

# Explore data

Work read-only unless the user explicitly requests a derived artifact.

1. Identify format, tables or sheets, row and column counts, types, units, keys,
   time range, and likely grain. State any inferred definitions.
2. Inspect a bounded representative sample, then compute full-data aggregates
   with DuckDB, pandas, or NumPy when feasible. Avoid loading a large dataset into
   memory when a scan or sampled query is sufficient.
3. Measure missingness, invalid values, duplicates, key uniqueness, category
   cardinality, and date or timezone consistency.
4. Summarize numeric and categorical distributions with suitable robust
   statistics. Check outliers, impossible bounds, discontinuities, and segment or
   time trends.
5. Test a small number of question-relevant relationships. Label exploration as
   descriptive, not causal.
6. Report data coverage, methods, key findings, anomalies, and limitations. Name
   the next analysis only when evidence justifies it.

Do not dump raw personal or secret values. Prefer counts, ranges, redacted
examples, and sufficiently large groups. Preserve the source and do not silently
coerce, fill, drop, or rewrite data.
