---
name: statistical-analysis
description: Apply descriptive statistics, uncertainty estimates, hypothesis tests, correlations, trend analysis, and outlier methods when a decision requires quantified evidence.
license: Apache-2.0
modified: Adapted for Core Agent; differs from the pinned upstream version.
---

# Statistical analysis

1. Define the question, population, sampling or assignment mechanism, outcome,
   comparison, analysis unit, and time window before selecting a method.
2. Inspect missingness, dependence, censoring, measurement quality, distribution
   shape, outliers, and sample sizes. Choose robust summaries when assumptions for
   mean and standard deviation are weak.
3. Select a method that matches the design and data. Check its assumptions; use a
   non-parametric, resampling, or descriptive alternative when they do not hold.
4. Report sample size, estimate, effect size, uncertainty or confidence interval,
   and the test statistic or p-value when applicable. Account for multiple
   comparisons and pre-existing subgroup searches.
5. Distinguish statistical evidence from practical importance. Do not claim
   causality from observational association without a defensible causal design.
6. Stress-test important conclusions across reasonable definitions, segments, or
   outlier treatments and disclose material sensitivity.

Avoid universal sample-size rules and false precision. If data or design cannot
support the requested inference, provide the strongest valid descriptive result
and state what additional evidence would be required.
