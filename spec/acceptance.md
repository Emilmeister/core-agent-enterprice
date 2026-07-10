# Критерии готовности целевого продукта

Этот документ проверяет итоговую спецификацию. Поставка может реализовать подмножество только через явный [release profile](releases/v1.md); реализованное поведение не может противоречить этим критериям.

## Публичный контракт и sessions

- [ ] Любой run принимает только `prompt`, `mcp`, `skills`; execution context передаётся отдельно.
- [ ] Stateless и stateful режимы используют один RunRequest и одинаковую event semantics.
- [ ] Session поддерживает create/get/close/export/delete и optimistic concurrency.
- [ ] Event stream упорядочен, resumable и заканчивается ровно одним terminal event.
- [ ] Control-команды idempotent, revision-aware и не расширяют RunRequest.

## Runtime и модели

- [ ] Capability negotiation отклоняет несовместимый model/adapter до первого turn.
- [ ] Router выбирает только models, разрешённые data, region, capability и budget policy.
- [ ] Fallback не повторяет tool call и compacts context перед переходом на меньшее окно.
- [ ] Pause/resume освобождают worker и продолжают run из checkpoint.
- [ ] Hard limits останавливают зацикливание, включая суммарный budget child runs.
- [ ] Raw chain-of-thought отсутствует в events, logs, audit, memory и tool arguments.

## Durable execution

- [ ] Run восстанавливается после crash из event log/checkpoint с той же revision.
- [ ] Pending approval и input восстанавливаются с прежними IDs.
- [ ] Idempotent call можно безопасно продолжить; неоднозначная мутация не повторяется.
- [ ] Lease не позволяет двум workers одновременно изменить один run.
- [ ] Checkpoints создаются на всех границах внешнего side effect и durable wait.

## Контекст

- [ ] Прогноз occupancy учитывает input, selected tool schemas и output reserve.
- [ ] При 80% compaction происходит до model call и снижает occupancy до 60% или ниже.
- [ ] Prompt, policy, approvals, active constraints, artifact refs и provenance остаются pinned.
- [ ] Два и более hierarchical compaction сохраняют goal и полный immutable transcript.
- [ ] Большой tool catalog раскрывает schemas прогрессивно, а не целиком.
- [ ] Непомещающиеся pinned data дают `CONTEXT_UNRECOVERABLE`, не silent truncation.

## Sessions и memory

- [ ] Transcript, working state, session memory и user memory хранятся раздельно.
- [ ] Retrieval соблюдает ACL, scope, TTL, tombstones и token budget.
- [ ] Каждый retrieved/written fact имеет provenance и воспроизводимый selection record.
- [ ] Конфликтующие facts не склеиваются молча.
- [ ] Пользователь может увидеть, исправить, экспортировать и удалить memory.
- [ ] Удаление инвалидирует indexes/caches и не позволяет записи снова попасть в context.
- [ ] Sensitive data и model speculation не становятся долгосрочной memory.

## Skills

- [ ] Discovery загружает metadata, activation — полный `SKILL.md`, resources — только по необходимости.
- [ ] Resolver фиксирует version, transitive dependencies, digests и signatures в lock snapshot.
- [ ] Изменение или yank package не меняет активный run.
- [ ] Capability/permission conflict обнаруживается до исполнения.
- [ ] Script проходит обычные sandbox, policy и approval checks.
- [ ] Path traversal, symlink escape и invalid signature отклоняются.

## Tools и MCP

- [ ] Tool arguments валидируются до policy и execution; незарегистрированный tool невозможен.
- [ ] Terminal ограничивает cwd, env, output, process tree и timeout.
- [ ] Filesystem mutations атомарны и не уничтожают чужие изменения.
- [ ] MCP согласует protocol capabilities и поддерживает tools/resources/prompts/sampling/elicitation через local policy.
- [ ] Catalog revision меняет snapshot только на safe point.
- [ ] Secret refs разрешаются adapter-ом и не видны модели или MCP сверх необходимого.
- [ ] Параллельные calls имеют доказанные зависимости/isolation и детерминированное merge.

## Human-in-the-loop

- [ ] Risky call не начинается до действительного approval.
- [ ] Approval связан с digest точных arguments; изменение требует новой оценки.
- [ ] Reusable grant ограничен identity, session, action, resource, arguments, expiry и revocation.
- [ ] Новая deny policy инвалидирует подходящий grant до следующего использования.
- [ ] `deny`/timeout возвращаются модели как результат, не маскируются под execution failure.
- [ ] Input request типизирован и не используется как скрытый approval.
- [ ] UI получает понятный фактический effect без секретов и chain-of-thought.

## Delegation

- [ ] Child run получает минимальные data/tools/skills и отдельный budget slice.
- [ ] Parent policy нельзя ослабить; tenant identity нельзя сменить.
- [ ] Depth/fan-out ограничены и учитываются в общем budget.
- [ ] Parent проверяет structured result с provenance до использования.
- [ ] Cancel parent корректно отменяет или orphan-policy обрабатывает children.

## Security, tenancy и lifecycle данных

- [ ] Prompt injection из file, skill или MCP не меняет host policy и не раскрывает secret.
- [ ] Files, processes, connections, stores, caches и artifact URLs tenant-isolated.
- [ ] Remote extensions проверяются по integrity/trust policy и поддерживают revocation.
- [ ] Retention/delete каскадно применяются к transcript, memory, checkpoints, artifacts и indexes.
- [ ] Все public errors безопасны, стабильны и имеют correlation ID.

## Observability и качество

- [ ] Audit восстанавливает model routes, tools, policy decisions, approvals, compactions, memory и recovery.
- [ ] Метрики имеют bounded cardinality и не содержат prompt/paths/arguments/IDs пользователя.
- [ ] Distributed trace связывает parent/child runs без утечки tenant data.
- [ ] Replay/eval измеряет task completion, compaction fidelity, tool correctness, policy errors, memory quality и duplicate side effects.
- [ ] Debug mode не отключает redaction.

## Сквозные сценарии

1. **Локальная задача:** prompt без extensions безопасно изменяет workspace, запускает проверку и возвращает итог.
2. **Длинная session:** несколько runs и compactions сохраняют decisions, но удалённая memory больше не извлекается.
3. **Внешнее действие:** MCP call ждёт approval, переживает restart и выполняется ровно один раз.
4. **Fallback:** provider падает, route меняется без потери tool state и дублирования side effect.
5. **Delegation:** parent распределяет независимое исследование, проверяет результаты и укладывается в общий budget.
6. **Враждебное расширение:** skill/MCP пытается повысить права и извлечь secret; policy блокирует это с полным audit.
7. **Multi-tenant:** конкурентные runs двух tenants не видят sessions, processes, artifacts, memory или events друг друга.
8. **Удаление:** subject deletion очищает все производные данные и retrieval indexes в пределах SLA.

## Definition of Done целевого продукта

- Все критерии и сквозные сценарии проходят в CI/системных evals.
- Public schemas и examples проверяются против единого источника типов.
- Каждая capability имеет failure-mode, policy и observability contract.
- Recovery/chaos tests доказывают отсутствие duplicate side effects.
- Security review охватывает model, MCP, skill, sandbox, tenancy и data lifecycle boundaries.
- Документация и последний стабильный release profile совпадают с реальным поведением.
