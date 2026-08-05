---
name: systematic-debugging
description: Diagnose technical failures, logs, tests, APIs, networks, Kubernetes, performance problems, or unexpected behavior by gathering evidence and tracing the root cause before recommending a fix.
license: MIT
---

# Systematic debugging

Use this workflow for a concrete technical failure or unexplained behavior. Do
not use it for a general design question with no observed problem.

1. State the symptom, expected behavior, affected scope, and current evidence.
2. Reproduce the issue with the smallest safe check available. If reproduction
   is impossible, say so and collect timestamps, error codes, versions, and
   boundary observations instead of guessing.
3. Trace inputs, state, and outputs across component boundaries until the first
   incorrect transition is found. Compare with a nearby working case.
4. Form one falsifiable hypothesis: cause, supporting evidence, and a check that
   would disprove it. Test one variable at a time.
5. Recommend or apply the smallest change at the source of the problem, within
   the available policy and write permissions.
6. Re-run the original reproduction and an appropriate regression check. Report
   the observed output, remaining uncertainty, and work not completed.

Never print credentials, broad environment dumps, private data, or unrestricted
logs merely to gather evidence. Prefer narrow, redacted queries. Do not mutate a
system during diagnosis unless the user requested it and an enabled tool permits
it. A failed hypothesis is evidence; update it rather than stacking speculative
fixes.

Read a supporting resource only when its situation applies:

- `references/root-cause-tracing.md` for a failure deep in a call or service chain;
- `references/condition-based-waiting.md` for timing, readiness, or flaky waits;
- `references/defense-in-depth.md` after finding a trust-boundary validation gap.
