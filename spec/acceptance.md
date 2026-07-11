# Критерии готовности целевого продукта

Поставка может реализовать подмножество только через явный [release profile](releases/v1.md); реализованное поведение не может противоречить этим критериям.

## A2A и публичный контракт

- [ ] Core Agent публикует валидную Agent Card с protocol/binding versions, auth, media types, skills и capabilities.
- [ ] Внешнее общение использует A2A Message/Task/Artifact; отдельная публичная task state machine отсутствует.
- [ ] Message Parts отображаются на `prompt`, required Core extension — только на `mcp`/`skills`.
- [ ] Неподдерживаемая required extension/version возвращает стандартно отображаемую A2A ошибку до model turn.
- [ ] A2A `contextId` сохраняет session, но MCP/skills не наследуются в новую Task неявно.
- [ ] Blocking, `return_immediately`, polling, subscription/streaming и push видят одну durable Task history.
- [ ] Disconnect stream не отменяет Task; at-least-once push update дедуплицируется.
- [ ] Internal states корректно отображаются только на стандартные A2A Task states.

## Kernel instructions

- [ ] KernelInstructions всегда присутствуют отдельно от AgentProfilePrompt и имеют version/digest в audit.
- [ ] AgentProfilePrompt, user Message, skill, memory или MCP output не могут отключить memory rules, mandatory tools, approvals, delegation, tasks, isolation или telemetry.
- [ ] Runtime enforcement отклоняет запрещённое действие, даже если model output просит обойти kernel instruction.
- [ ] Child получает ту же kernel version и не может ослабить parent/host policy.
- [ ] Raw chain-of-thought отсутствует в A2A, telemetry, audit, memory и tool arguments.

## Runtime и durability

- [ ] Capability negotiation отклоняет несовместимый model/adapter до первого turn.
- [ ] Model fallback не повторяет tool call и compacts context перед меньшим окном.
- [ ] Pause/passive wait освобождают model worker и продолжаются из checkpoint/notification.
- [ ] Lease не позволяет двум workers одновременно изменить Task.
- [ ] Pending approval, input и task notifications восстанавливаются с прежними IDs/revisions.
- [ ] Idempotent operation можно продолжить; неоднозначная мутация не повторяется.
- [ ] Hard limits включают parent и все child/background Tasks.

## Context и compaction

- [ ] Base tokens считаются как system/kernel/profile + selected tool schemas + output reserve.
- [ ] Working occupancy учитывает только prompt/history/summaries/memory/notifications/artifact excerpts относительно оставшейся working capacity.
- [ ] System prompt и tool schemas не входят в 90%/10–15% threshold.
- [ ] Ниже 90% working occupancy compaction не запускается; при 90% и выше происходит до model call.
- [ ] После compaction working occupancy находится в диапазоне 10–15%.
- [ ] Prompt, policy, approvals, task contracts, active constraints, artifact refs и memory provenance остаются pinned.
- [ ] Повторные compactions сохраняют goal и immutable transcript mapping.
- [ ] Непомещающиеся protected/pinned data дают `CONTEXT_UNRECOVERABLE`, не silent truncation.

## Markdown memory и file lifecycle

- [ ] Markdown corpus является source of truth; BM25/vector/graph indexes полностью перестраиваются из него.
- [ ] Memory root нельзя менять terminal/filesystem tool-ом в обход `core.memory.*`.
- [ ] Перед create/update agent выполняет hybrid search и проверяет top candidates.
- [ ] Та же тема обновляет существующий file; новый subject/scope создаёт новый file.
- [ ] Committed file не превышает 200 body lines; прогноз 201+ отклоняется и предлагает atomic split.
- [ ] Split сохраняет stable IDs/aliases, ссылки и provenance без разрыва semantic block.
- [ ] Update использует expected revision; concurrent conflict не разрешается last-write-wins.
- [ ] Delete/tombstone исключает content из Markdown, summaries, BM25, vectors, graph и caches.

## Indexing, NER и graph

- [ ] Create/update/split/delete обновляет chunking, BM25, embeddings, NER, entity resolution и graph до atomic publication revision.
- [ ] Изменённый chunk удаляет stale mentions/relations; неизменённый сохраняет stable chunk ID/index data.
- [ ] Partial index failure не делает staging Markdown видимым обычному search.
- [ ] NER/relation edges имеют evidence line/chunk, confidence, extractor version и source revision.
- [ ] Low-confidence entity merge остаётся candidate; manual correction переживает full reindex.
- [ ] Изменение extractor/taxonomy строит новую revision в фоне и атомарно переключает её.

## Hybrid search и rerank

- [ ] Candidate generation независимо получает BM25, embedding и bounded graph candidates.
- [ ] Query NER/entity linking выполняется до graph traversal.
- [ ] Raw scores разных retrievers не складываются без calibration/RRF.
- [ ] Reranker получает text, heading, все component scores/ranks, graph paths, recency и provenance.
- [ ] Search result возвращает component/final scores, revision и provenance.
- [ ] Недоступный channel явно помечает degraded search; конфликтующие claims не скрываются ranking-ом.
- [ ] Candidate set, model/index/reranker versions позволяют воспроизвести retrieval decision.

## Background Tasks

- [ ] `task.start` возвращает handle сразу, пока main agent продолжает независимую работу.
- [ ] Completion/failure/artifact/input notifications durable, versioned и at-least-once.
- [ ] Notification попадает в model context только на safe boundary и дедуплицируется.
- [ ] `task.wait` не создаёт busy polling и не удерживает model worker/execution environment.
- [ ] Agent может ничего не делать до notification или timeout.
- [ ] Cancel/timeout завершает process tree; orphan policy обрабатывает Task после terminal parent.
- [ ] Parent не завершает зависимый итог, пока required Task pending.

## Сабагенты

- [ ] Сабагент создаётся как неблокирующая A2A Task.
- [ ] Delegation contract содержит узкую instruction, exact tool/MCP/skill allowlists, memory policy, budget и result schema.
- [ ] Child не видит невыданные рабочие capabilities даже на discovery.
- [ ] Mandatory kernel memory/task/audit tools добавляются runtime-ом и не удаляются parent-ом.
- [ ] Child и main читают одну committed session Markdown-memory revision stream.
- [ ] Child memory write проходит тот же revision/index/NER pipeline и уведомляет parent.
- [ ] Parent проверяет Artifact/result provenance; child не общается наружу без capability.
- [ ] Depth/fan-out и child budgets ограничены общим parent budget.

## Execution environment

- [ ] Ни одна terminal command, skill script, stdio MCP или child command не запускается control-plane host subprocess-ом.
- [ ] Environment имеет отдельные filesystem/process/user/network boundaries, immutable image и resource limits.
- [ ] Host root, runtime socket, SSH agent, metadata endpoint и control-plane credentials недоступны.
- [ ] Parent/child получают отдельные copy-on-write overlays; merge использует base revision/conflict detection.
- [ ] Egress default-deny проверяет DNS, resolved IP и redirect.
- [ ] Secret инжектируется только в разрешённый call и не попадает в image/checkpoint/telemetry/artifact.
- [ ] Недоступность isolation даёт `EXECUTION_ENVIRONMENT_UNAVAILABLE`; host fallback отсутствует.
- [ ] Teardown уничтожает process tree/writable layer или фиксирует cleanup failure.

## Tools, MCP и human-in-the-loop

- [ ] Tool arguments валидируются до policy и execution.
- [ ] MCP tools/resources/prompts/sampling/elicitation проходят local policy независимо от server metadata.
- [ ] Risky call не начинается до действительного approval точных arguments.
- [ ] Reusable grant ограничен identity/session/action/resource/arguments/expiry/revocation.
- [ ] Input request не используется как скрытый approval; auth использует отдельный state.
- [ ] A2A status/Message показывает понятный effect без secret/chain-of-thought.

## OpenTelemetry

- [ ] Traces, metrics и logs создаются OTel SDK и экспортируются OTLP.
- [ ] W3C Trace Context проходит через A2A, queue, MCP, sandbox RPC и remote subagents без влияния на authorization.
- [ ] Durable/background Task использует новый execution trace со Span Link на submission, а не многочасовой request span.
- [ ] Model, policy, tool, execution, MCP, memory BM25/vector/graph/rerank/NER и subagent operations имеют spans.
- [ ] OTel semantic-convention version pinned; custom attributes используют `core_agent.*`.
- [ ] Content/arguments/results/system instructions выключены в telemetry по умолчанию.
- [ ] Metric labels bounded и не содержат IDs, prompt, path, command, entity или memory text.
- [ ] Collector outage не повреждает Task/audit; telemetry drops наблюдаемы.

## Security, tenancy и data lifecycle

- [ ] Prompt injection из profile/file/skill/MCP/A2A peer не меняет kernel/host policy и не раскрывает secret.
- [ ] Tasks, environments, connections, Markdown, indexes, graph, artifacts и telemetry tenant-isolated.
- [ ] Remote extensions проверяются по integrity/trust/revocation policy.
- [ ] Retention/delete каскадно применяются к transcript, memory, checkpoints, artifacts, derived indexes и eval data.
- [ ] Public errors безопасны, стабильны и отображаются в A2A semantics.

## Сквозные сценарии

1. **A2A background:** клиент отправляет Message с `return_immediately`, закрывает stream и позже получает тот же Task result через subscribe/push.
2. **Compaction:** working context достигает 90%, сжимается до 10–15%, не считая system/tools, и сохраняет pending Task/approval.
3. **Memory update:** agent находит существующий Markdown file, обновляет его, NER удаляет stale edge, hybrid search возвращает новую revision.
4. **Memory split:** update превысил бы 200 строк; atomic split создаёт index/children без duplicate claims и битых links.
5. **Shared memory:** child обновляет общую memory, parent получает notification и читает committed indexed revision.
6. **Focused delegation:** child видит только перечисленные tools/skills плюс mandatory kernel tools и возвращает schema-valid Artifact.
7. **Concurrent work:** main продолжает задачу, пока два child/background Tasks выполняются, затем обрабатывает notifications без busy polling.
8. **Host isolation:** враждебная команда не видит host filesystem/socket/metadata и не может обойти memory tools.
9. **OTel causality:** A2A submit, background Task, child, model, memory, tool и sandbox находятся в связанных traces без content leakage.
10. **Внешнее действие:** MCP write ждёт approval, переживает recovery и выполняется ровно один раз.

## Definition of Done

- Все критерии и сквозные сценарии проходят в CI, integration, chaos и eval suites.
- A2A/Core extension schemas и examples проверяются против одного источника типов.
- Recovery tests доказывают отсутствие duplicate side effects и потерянных notifications.
- Memory rebuild test удаляет derived indexes и получает эквивалентный searchable graph из Markdown.
- Security review охватывает A2A, model, MCP, skills, memory graph, execution plane, subagents, OTel и tenancy.
