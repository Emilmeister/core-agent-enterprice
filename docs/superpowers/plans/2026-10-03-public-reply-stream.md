# Public reply streaming implementation plan

**Goal:** Stream the actual final model response to owner UI and scoped A2A clients, including workflow continuations.

**Architecture:** A main-run model-only `core_response_begin({})` commits the answer phase with ordinary tool accounting. Its next ordinary model call has no tools and streams public text; the emergency reserved finalizer remains incomplete. One bounded process-local reply hub retains cumulative previews. Canonical workflow, Task, history and final artifacts retain their current durability and authority.

- [x] Extend normative A2A/runtime/tool specifications with the accepted phase, budget and preview boundaries.
- [x] Add existing-suite regressions for the control boundary, budget charge, supersession and bounded tenant/task hub; run with `uv run python -m unittest` and observe failures.
- [x] Implement the tool-free phase in runtime/kernel, excluding children, direct/nested invocation and work/tool/hidden reasoning from the public sink.
- [x] Connect the hub to initial SDK streams and passive scoped Subscribe, including recovery workers; retain persisted Task first and terminal last.
- [x] Prove prefix delivery before provider completion over real TCP, then exercise follow-up/retry/recovery and PostgreSQL persistence in an isolated database.
- [x] Run applicable existing backend checks with `uv`, then hand off to root for hash lock, release gates and commit.

Public message metadata is `{partial:true,core_agent_stream:{version:1,generation:positive_model_turn,sequence:positive_integer,superseded?:true}}`. Generations are monotone through durable model accounting; superseded generations cannot publish again. Previews are bounded to 256KiB UTF-8 per Task and 64 cached Tasks; eviction/restart may lose a preview. No preview enters history or push. A complete canonical artifact replaces the preview.

Backend handoff proof: 264 targeted existing-suite tests passed (18 PostgreSQL cases skipped in that run); 17 PostgreSQL cases passed separately in a disposable database which was dropped afterward. Scoped Ruff and `git diff --check` passed. Independent review and its 11-test rerun confirmed partial status suppression, tools-free budget sizing, lease-fenced supersession, slow-client coalescing and eviction-safe follow-up closure. Full release suite, frozen hash refresh and native browser gate remain with the root agent.

Final release verification: the full existing suite passed 1749 tests with four dedicated-fixture skips; the separately required native browser gate passed against real PostgreSQL, Keycloak and a Bubblewrap Pod. It proved delivery while the model was blocked, exclusion of reasoning, reopening with canonical history and replacement by one safely rendered final reply. Ruff, UI typecheck/build, package build and frozen specification checks passed. The multi-architecture deployment image is pinned by immutable digest in the deployment repository.
