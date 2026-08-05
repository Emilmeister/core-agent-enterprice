---
name: verification-before-completion
description: Verify claims that work, analysis, queries, actions, or fixes are complete and correct using fresh authoritative evidence before giving a final answer.
license: MIT
---

# Verification before completion

Before claiming success:

1. Translate the requested result into observable acceptance checks.
2. Choose the narrowest authoritative check for each claim: command exit status,
   test result, query output, API state, document content, calculation, or task
   status.
3. Run the check now using enabled tools and read the complete relevant output.
   A prior run, a plausible implementation, or another agent's success message is
   not evidence.
4. Compare the evidence with every acceptance check. Investigate discrepancies;
   do not reinterpret them as success.
5. Report what was verified, the evidence used, and any residual risk.

Use checks proportional to impact: a focused reproduction for a narrow fix, plus
the applicable broader gate when shared behavior changed. Do not perform a
mutation solely to verify a read-only task.

If a tool, source, permission, deadline, or budget prevents verification, do not
invent the result. Return the verified intermediate result, explicitly mark the
task incomplete, and state the checks and work you intended but could not finish.
