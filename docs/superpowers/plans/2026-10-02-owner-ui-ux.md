# Owner UI UX implementation plan

Goal: implement the approved UI/UX feedback, excluding technical mode and batch
settings saves. Normative behavior is in public-contract and ENT-AC-74–80.

1. Preserve existing scope/auth/CAS/guardrails/durable-state guarantees. Add
   company-scoped chat titles and original display text with additive migration
   and owner API regression proof; retain old cursor and task identities.
2. Build one card per Task/tool call, grouped successful activity, safe output
   disclosure and unified input/output file cards with authenticated previews.
3. Make active approvals concrete and tool policies compact/searchable/filterable,
   with positive guardrail checkbox and explicit per-rule saves.
4. Integrate named navigation/header, runtime-derived status, active-request jump,
   separated file panel, compact growing composer and stable scroll/focus behavior.
5. Update existing browser acceptance for UI selectors and exercise all approved
   behavior with real Keycloak/PostgreSQL/native sandbox; run normal unit/PG,
   frontend/package/release checks, review and save validated changes.
6. Build/push amd64/arm64 image, update the existing deployment branch pins,
   deploy with the authorized script and verify actual UI. Merge verified Core
   branch into local main; preserve deployment branch and user .idea files.

Independent implementation domains: backend chat/history metadata; history/action/
file presentation; interaction/policy presentation. Root owns shared types,
Chat/App, panel integration, styles, specifications, browser gates and deployment.
