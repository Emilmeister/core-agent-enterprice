# Guardrail concurrency and response schema

**Goal:** Allow five simultaneous classifier requests and require a structured
verdict from the configured provider.

**Architecture:** Keep the shared classifier and existing material-review flow.
Increase its bounded semaphore to five. Configure the classifier's private HTTP
adapter with a provider-native response schema; leave the main model unchanged.

**Stack:** Python, threading, the existing HTTP model adapter and unittest suite.

## Agreed behavior

- Five provider calls may run at once per serving process, across chats and
  subagents. A sixth call immediately returns `unverified/detector_busy` and
  enters the existing owner review flow, without charging a provider attempt.
- A timed-out provider call occupies its slot until it actually exits. Its late
  result cannot release material. Existing deadlines and durable budgets apply.
- The response schema is an object with exactly one required string, `verdict`,
  whose allowed values are `clear`, `suspicious` and `uncertain`.
- OpenAI-compatible requests use `response_format.json_schema` with
  `strict: true`; Anthropic requests use `output_config.format` with a JSON schema.
  A provider rejecting the schema follows the existing failure/owner-review
  path. Do not silently retry without a schema or weaken local validation.
- Inherited response formats cannot replace the classifier schema. Configuring
  the detector must not mutate the main model's provider options.

## Implementation and verification

- [x] Obtain the required permission for frozen spec/tests/hash edits.
- [x] Update the guardrail contract and ENT-AC-41/45 acceptance criteria.
- [x] Extend existing guardrail/configuration tests and observe failure for the
  old one-slot implementation and missing wire schema.
- [x] Update `core_agent/guardrails.py` and `_guardrail_classifier` in
  `core_agent/app.py`, without adding dependencies or new deployment settings.
- [x] Verify simultaneous capacity, saturation, timeout slot retention,
  subsequent slot reuse, and provider wire schemas for both configuration paths.
- [x] Run affected tests, Ruff, and the complete unittest suite with PostgreSQL;
  update implementation status, release notes, AGENTS.md and spec hashes.
- [x] Independent review completed; change ready for commit. Deployment is a
  separate step.

## Verification

- Before implementation, the affected tests failed on the one-slot limit and
  missing/overridden schema. After implementation: 128 targeted tests passed.
- Full unittest suite with disposable PostgreSQL: 2094 tests, no failures,
  5 skips. Native Docker restart, actual-browser/Pod and Keycloak integration
  fixtures were not configured; the suite also retains platform/scoped skips.
- Ruff passed. No dependencies, provider credentials, or live deployment were
  changed. Provider schema acceptance was checked with controlled adapters;
  compatibility with a particular deployed model requires its own smoke check.

Anthropic wire format follows the official
[structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
