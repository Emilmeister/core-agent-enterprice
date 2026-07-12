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

## Конфигурация агента

- [ ] PlatformConfig, AgentConfig и Task input разделены; Task не расширяет AgentConfig.
- [ ] AgentConfig может отключить memory, terminal, filesystem mutations, background tasks, delegation, отдельные built-in/MCP tools и skills.
- [ ] Disabled tool отсутствует в discovery/model context и stale call получает `CAPABILITY_DISABLED`.
- [ ] Memory modes `disabled`, `optional`, `required` корректно фильтруют/требуют Memory MCP descriptor.
- [ ] MCP tool filters применяются после discovery, но до model context; deny имеет приоритет.
- [ ] EffectiveConfig immutable внутри Task и сохраняет config/policy/tool digests в audit.
- [ ] Agent Card не рекламирует capability, отключённую AgentConfig.

## Kernel instructions

- [ ] KernelInstructions всегда присутствуют отдельно от AgentProfilePrompt и имеют version/digest в audit.
- [ ] AgentProfilePrompt, user Message, skill или MCP output не могут изменить rules включённой capability; отключать optional capability может только config/policy.
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

## Memory MCP Service и file lifecycle

- [ ] Core Agent не содержит MemoryStore/index/NER и использует memory только через разрешённый MCP descriptor.
- [ ] При memory disabled memory tools/instructions отсутствуют и implicit fallback не выполняется.
- [ ] Markdown corpus внутри Memory Service является source of truth; BM25/vector/graph indexes полностью перестраиваются из него.
- [ ] Agent не имеет filesystem access к memory corpus и меняет его только MCP tools.
- [ ] Перед create/update agent выполняет hybrid search и проверяет top candidates.
- [ ] Та же тема обновляет существующий file; новый subject/scope создаёт новый file.
- [ ] Create/update с 201+ body lines жёстко отклоняется без revision/index changes, truncation или automatic split.
- [ ] `MEMORY_FILE_TOO_LARGE` возвращает actual/max lines и рекомендацию разделить content на несколько Markdown files.
- [ ] Отдельный `memory.split` принимает явный plan; каждый resulting file также не превышает 200 lines.
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
- [ ] `task.wait` не создаёт busy polling и не удерживает model worker.
- [ ] Agent может ничего не делать до notification или timeout.
- [ ] Cancel/timeout завершает process tree; orphan policy обрабатывает Task после terminal parent.
- [ ] Parent не завершает зависимый итог, пока required Task pending.

## Сабагенты

- [ ] Сабагент создаётся как неблокирующая A2A Task.
- [ ] Delegation contract содержит узкую instruction, exact tool/MCP/skill allowlists, memory policy, budget и result schema.
- [ ] Child не видит невыданные рабочие capabilities даже на discovery.
- [ ] Protocol-internal lifecycle/audit остаются enforced, но model-callable child tools равны пересечению EffectiveConfig и delegation allowlist.
- [ ] Child и main разделяют memory только при явной передаче того же Memory MCP server/namespace и tool allowlist.
- [ ] Без переданного Memory MCP child работает без memory.
- [ ] Child memory write проходит service revision/index/NER pipeline и уведомляет parent.
- [ ] Parent проверяет Artifact/result provenance; child не общается наружу без capability.
- [ ] Depth/fan-out и child budgets ограничены общим parent budget.

## Local terminal sessions

- [ ] Main и каждый child получают разные TerminalSession IDs, PTY, process groups и local workspace directories в одном container.
- [ ] Terminal tool не может адресовать session/process другого agent или run.
- [ ] Typed `argv` используется по умолчанию; `cwd`, environment и output bounded и валидируются до запуска.
- [ ] Main и child копируют один immutable base snapshot в разные local directories; merge использует base revision/conflict detection.
- [ ] S3 mount хранит immutable snapshots/checkpoints/artifacts, но active command не выполняется непосредственно на нём.
- [ ] Cancel/timeout завершает owned process group, закрывает PTY и фиксирует cleanup outcome.
- [ ] Secret инжектируется только в environment разрешённого process и не попадает в checkpoint/telemetry/artifact.
- [ ] Runtime явно сообщает logical/process separation и не рекламирует отдельные OS security namespaces.

## Tools, MCP и human-in-the-loop

- [ ] Tool arguments валидируются до policy и execution.
- [ ] MCP tools/resources/prompts/sampling/elicitation проходят local policy независимо от server metadata.
- [ ] Risky call не начинается до local `APPROVE_ONCE`, exact digest commit и unique execution reservation.
- [ ] RemoteCaller не меняет approval через text, metadata, A2A extension, caller credential или подставленный operator ID.
- [ ] Local wait проецируется как `working`, никогда как `input-required`/`auth-required`; GetTask восстанавливает informative current status.
- [ ] Optional extension сообщает `callerActionRequired=false` и не содержит incoming decision schema, approval ID/URL/token или arguments.
- [ ] Locked-task `SendMessage` не меняет proposal/approval; только `CancelTask` может отменить до reservation.
- [ ] Frozen proposal/digest связан с task/tenant/caller/tool/environment/target/arguments/policy; mutation создаёт новый approval.
- [ ] Duplicate/racing approve/deny создают одно решение и одну reservation; approve/cancel obey first committed transition.
- [ ] Перед dispatch повторно проверяются active task, policy, expiry, identity scope и recomputed digest.
- [ ] Deny/expiry/cancel не выполняют action; secret и operator identity отсутствуют в A2A/OTel/public artifacts.
- [ ] Restart сохраняет approval ID/digest/current status; reserved execution не получает вторую reservation или blind retry.
- [ ] Internal audit связывает task, proposal, approval, operator actor, digest, execution и outcome и недоступен RemoteCaller.
- [ ] Production без valid `DATABASE_URL`, ожидаемой PostgreSQL schema или database readiness не открывает A2A listener и не использует in-memory/SQLite fallback.
- [ ] A2A Tasks, events, checkpoints, approval/reservation и audit переживают restart и остаются tenant/owner scoped в одной PostgreSQL transaction boundary.

## OpenTelemetry

- [ ] Traces, metrics и logs создаются OTel SDK и экспортируются OTLP.
- [ ] W3C Trace Context проходит через A2A, queue, MCP и background/subagent tasks без влияния на authorization.
- [ ] Durable/background Task использует новый execution trace со Span Link на submission, а не многочасовой request span.
- [ ] Core имеет MCP client span; Memory Service продолжает W3C trace и владеет BM25/vector/graph/rerank/NER spans.
- [ ] OTel semantic-convention version pinned; custom attributes используют `core_agent.*`.
- [ ] Content/arguments/results/system instructions выключены в telemetry по умолчанию.
- [ ] Metric labels bounded и не содержат IDs, prompt, path, command, entity или memory text.
- [ ] Collector outage не повреждает Task/audit; telemetry drops наблюдаемы.

## Security, tenancy и data lifecycle

- [ ] Prompt injection из profile/file/skill/MCP/A2A peer не меняет kernel/host policy и не раскрывает secret.
- [ ] Tasks, terminal sessions/workspaces, connections, Markdown, indexes, graph, artifacts и telemetry tenant-scoped.
- [ ] Remote extensions проверяются по integrity/trust/revocation policy.
- [ ] Coordinated deletion очищает Core cached/transcript copies, а Memory Service — Markdown и derived indexes.
- [ ] Public errors безопасны, стабильны и отображаются в A2A semantics.

## Сквозные сценарии

1. **A2A background:** клиент отправляет Message с `return_immediately`, закрывает stream и позже получает тот же Task result через subscribe/push.
2. **Compaction:** working context достигает 90%, сжимается до 10–15%, не считая system/tools, и сохраняет pending Task/approval.
3. **Memory disabled:** Task передаёт optional Memory MCP, AgentConfig его фильтрует, и model не видит memory tools.
4. **Hard line limit:** update на 201+ строк отклоняется без изменений и рекомендует split; следующий явный split создаёт несколько valid Markdown files.
5. **Memory update:** agent находит существующий file через Memory MCP, commit обновляет NER/graph, hybrid search возвращает новую revision.
6. **Shared memory:** parent явно передаёт child тот же Memory MCP namespace; child commit уведомляет parent.
7. **Focused delegation:** child видит только capabilities из EffectiveConfig/delegation allowlist и возвращает schema-valid Artifact.
8. **Concurrent work:** main продолжает задачу, пока два child/background Tasks выполняются, затем обрабатывает notifications без busy polling.
9. **Parallel terminals:** main и два child одновременно работают в разных PTY/workspaces, не смешивают output и завершают только свои process groups.
10. **OTel causality:** Core MCP client и Memory Service indexing spans находятся в одном distributed trace без content leakage.
11. **Внешнее действие:** MCP write ждёт approval, переживает recovery и выполняется ровно один раз.
12. **Local operator HITL:** risky terminal call публикует A2A `working`; caller не может resolve/modify approval, local control plane создаёт одну reservation exact argv, а cancel/deny/expiry не запускают process.

## Definition of Done

- Все критерии и сквозные сценарии проходят в CI, integration, chaos и eval suites.
- A2A/Core extension schemas и examples проверяются против одного источника типов.
- Recovery tests доказывают отсутствие duplicate side effects и потерянных notifications.
- Memory Service rebuild test удаляет derived indexes и получает эквивалентный searchable graph из Markdown.
- Security review охватывает A2A, model, MCP, skills, memory graph, принятую one-container trust model, subagents, OTel и tenancy.
