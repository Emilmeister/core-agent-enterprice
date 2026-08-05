---
name: validate-data
description: Audit an existing analysis, SQL result, spreadsheet, calculation, or chart for data quality, logic errors, reproducibility, and unsupported conclusions.
license: Apache-2.0
modified: Adapted for Core Agent; differs from the pinned upstream version.
---

# Validate data and analysis

Validate independently and read-only:

1. Inventory inputs, definitions, filters, transformations, joins, formulas,
   outputs, and claims. Identify what cannot be reproduced from available data.
2. Recompute the most decision-relevant totals, denominators, rates, and samples
   by a second method where practical.
3. Check row counts and join cardinality; duplicate amplification; missing values;
   units, currencies, timezones, date boundaries; cohort and population drift;
   leakage; and averages of aggregates.
4. Verify that tables and charts match their sources, labels, scales, units, and
   stated time ranges.
5. Challenge statistical assumptions, causal language, sample size, multiple
   comparisons, and whether conclusions follow from the evidence.
6. Report findings by severity with the exact affected claim, evidence, impact,
   and minimal correction. Also state which checks passed and which could not be
   performed.

Do not edit the source to make validation pass. Do not expose row-level personal
data in the report. A plausible number is not a validated number.
