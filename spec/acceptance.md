# Критерии готовности целевого продукта

Поставка может реализовать подмножество только через явный [release profile](releases/v1.md); реализованное поведение не может противоречить этим критериям.

## A2A и публичный контракт

- [ ] Core Agent публикует валидную Agent Card с protocol/binding versions, auth, media types, skills и capabilities.
- [ ] Карточка доступна и по `/.well-known/agent-card.json`, и по историческому `/.well-known/agent.json`; оба пути возвращают идентичный документ, а query-параметры игнорируются.
- [ ] Каждая объявленная в карточке пара binding/version принимается соответствующим endpoint: `JSONRPC` отвечает на `0.3`, `HTTP+JSON` — на `1.0`, и карточка не содержит пар, которых нет.
- [ ] При заданном `AGENT_URL` карточка объявляет именно его, и никакой заголовок запроса это не меняет; `URL_AGENT` принимается как то же значение, а при обоих заданных выигрывает `AGENT_URL`.
- [ ] Без `AGENT_URL` карточка объявляет адрес из `X-Forwarded-Proto`/`X-Forwarded-Host`, иначе из схемы и `Host`; непригодное значение отбрасывается, а запрос карточки всё равно завершается успешно.
- [ ] Лишний член верхнего уровня JSON-RPC конверта не отклоняет запрос, не интерпретируется как session id и попадает в предупреждение с именами удалённых полей.
- [ ] Внешнее общение использует A2A Message/Task/Artifact; отдельная публичная task state machine отсутствует.
- [ ] Message Parts отображаются на `prompt`; собственное расширение протокола не требуется и не объявляется в Agent Card.
- [ ] Неподдерживаемая required extension/version возвращает стандартно отображаемую A2A ошибку до model turn.
- [ ] A2A `contextId` сохраняет session, но MCP/skills не наследуются в новую Task неявно.
- [ ] Blocking, `return_immediately`, polling, subscription/streaming и push видят одну durable Task history.
- [ ] Disconnect stream не отменяет Task; at-least-once push update дедуплицируется.
- [ ] Follow-up Message с существующим non-terminal `taskId` возвращает ту же Task, durable переживает restart и попадает отдельным user turn перед следующим model call.
- [ ] Duplicate `messageId` не доставляется дважды; concurrent Messages получают стабильный committed order; context/task mismatch и cross-tenant ID отклоняются.
- [ ] Уже начатый model/tool/side-effect call не прерывается; completion atomically проигрывает более раннему accepted Message, поэтому подтверждённый input не теряется.
- [ ] Follow-up не изменяет EffectiveConfig/MCP/skills/budgets.
- [ ] Internal states корректно отображаются только на стандартные A2A Task states.

## Конфигурация агента

- [ ] PlatformConfig, AgentConfig и Task input разделены; Task не расширяет AgentConfig.
- [ ] AgentConfig может отключить memory, terminal, filesystem mutations, background tasks, delegation, отдельные built-in/MCP tools и skills.
- [ ] Disabled tool отсутствует в discovery/model context и stale call получает `CAPABILITY_DISABLED`.
- [ ] Memory modes `disabled`, `optional`, `required` корректно фильтруют/требуют Memory MCP descriptor.
- [ ] MCP tool filters применяются после discovery, но до model context; deny имеет приоритет.
- [ ] EffectiveConfig immutable внутри Task и сохраняет config/policy/tool digests в audit.
- [ ] Agent Card не рекламирует capability, отключённую AgentConfig.
- [ ] Присутствующая, но пустая deployment-переменная трактуется как отсутствующая и получает документированный default вместо пустого значения.
- [ ] Невалидная конфигурация не поднимает listener, завершает процесс ненулевым кодом и печатает ровно одну строку с именем настройки и стабильным кодом, без traceback и без значений secret-переменных.

## Kernel instructions

- [ ] KernelInstructions всегда присутствуют отдельно от AgentProfilePrompt и имеют version/digest в audit.
- [ ] Пустой AgentProfilePrompt не добавляет generic system instruction; фактический prompt присутствует только как отдельный user Message/context item.
- [ ] Base kernel выбирает tool по смыслу задачи, не требует tool без материальной пользы и не дублирует conditional capability/tool-description semantics.
- [ ] AgentProfilePrompt, user Message, skill или MCP output не могут изменить rules включённой capability; отключать optional capability может только config/policy.
- [ ] Runtime enforcement отклоняет запрещённое действие, даже если model output просит обойти kernel instruction.
- [ ] Child получает ту же kernel version и не может ослабить parent/host policy.
- [ ] Provider-visible reasoning отсутствует в audit, memory и tool arguments; в A2A stream оно появляется только как помеченная `adk_thought` часть при `A2A_STREAMING_ENABLED=true` и вырезается из терминального кадра, а hidden/opaque reasoning не экспортируется никогда.
- [ ] Streaming-кадры несут reasoning, function_call и function_response с ADK-совместимыми метками, текст идёт кумулятивными снимками с `partial`, и поток заканчивается ровно одним `final:true`.

## Runtime и durability

- [ ] `THINKING_LEVEL` валидируется до первого turn, отображается в model invocation parameters и маппится в нативный OpenAI-compatible/Anthropic request без управления через RunRequest.
- [ ] Provider adapter отделяет visible reasoning от публичного ответа, сохраняет необходимый provider replay для следующего tool turn и экспортирует известный reasoning token usage.
- [ ] Capability negotiation отклоняет несовместимый model/adapter до первого turn.
- [ ] Model fallback не повторяет tool call и compacts context перед меньшим окном.
- [ ] Pause/passive wait освобождают model worker и продолжаются из checkpoint/notification.
- [ ] Lease не позволяет двум workers одновременно изменить Task.
- [ ] Lease регулярно продлевается во время model/tool/join работы дольше одного TTL; длинный joined child не завершает исправный parent с `LEASE_LOST`.
- [ ] Pending input и task notifications восстанавливаются с прежними IDs/revisions.
- [ ] Idempotent operation можно продолжить; неоднозначная мутация не повторяется.
- [ ] Hard limits включают parent и все child/background Tasks.
- [ ] Последний model turn заранее удержан внутри общего hard budget и вызывается с пустым tool catalog только для честного финального ответа; provider calls никогда не превышают limit.
- [ ] Charge каждого initial/retry provider attempt, local usage и pre-dispatch marker коммитятся атомарно; crash не создаёт бесплатный повтор, а неизвестный reserved finalizer не dispatch-ится второй раз.
- [ ] Charge model-issued tool request, local tool usage и pre-dispatch marker коммитятся атомарно; crash не списывает и не dispatch-ит один queued request повторно, rollback не расходится с root ledger.
- [ ] При исчерпанном tool budget ни один оставшийся call из assistant batch не dispatch-ится, каждый получает structured `BUDGET_EXCEEDED` с `tool_calls`, used/limit и указанием передать промежуточный результат выше.
- [ ] Исчерпание execution budget завершает Task как `completed` с `completion_reason=budget_exhausted`, `complete=false`, фактическим usage и текстом, разделяющим проверенное и незавершённое; Task не переходит в `failed` и модель не выдумывает недостающие results.
- [ ] Если финализирующая модель не вернула text или после retry недоступна, runtime публикует только детерминированное сообщение о неполноте без hidden reasoning/raw tool output; ambiguous mutation по-прежнему даёт `SIDE_EFFECT_UNKNOWN`.
- [ ] Follow-up, принятый во время reserved finalizer, durable попадает в transcript, не вызывает второй provider call сверх hard limit и явно помечается как необработанный в terminal partial result.
- [ ] Неиспользованный finalization reserve возвращается общему ledger атомарно с нормальным terminal transition; неудавшийся start child не оставляет ни task/workflow, ни занятую ёмкость.
- [ ] Scheduler handle, child workflow и его finalization reserve коммитятся одной транзакцией до запуска worker; ошибка admission не оставляет ни одной из трёх записей.
- [ ] Durable scheduler claim принадлежит одному worker, продлевается heartbeat-ом и не позволяет concurrent recovery запустить один contract дважды или stale worker записать terminal state.
- [ ] Live workflow lease исключает run из recovery даже при старте другой replica; совпадающий worker ID не заменяет живой token, а истёкшие workflow/scheduler tokens не renew-ятся и не terminalize-ят state; PostgreSQL expiry вычисляется по текущему server clock после ожидания row lock, независимо от clock skew replica и времени начала statement.
- [ ] Workflow transition повторно проверяет lease token/expiry на финальном `core_runs` update после ожидания shared budget или других transaction locks; lock wait через TTL даёт `LEASE_LOST` и откатывает весь transition/charge.
- [ ] Mutating exception после committed intent, resume/recovery или cancel workflow в `EXECUTING` дают `SIDE_EFFECT_UNKNOWN` без redispatch и без ложного `CANCELLED`, включая scheduler notification.
- [ ] При restart `cancel_requested` recoverable Task согласует durable child state один раз, а non-recoverable mutating Task получает reconciliation error вместо ложного `canceled` outcome; cancel между scan и claim либо сразу после claim не обходит reconciliation и не стирает её ошибку.
- [ ] Cancelled child атомарно возвращает неиспользованный finalization reserve; начатый finalizer остаётся учтённым как provider call.
- [ ] Budget finalization ждёт owned Tasks только bounded grace; некооперативная Task остаётся durable/cancel-requested, а partial result возвращается и перечисляет её в `pending_tasks` без выдуманного outcome.
- [ ] Persisted result различает local `usage` и общий `shared_budget`; live и crash-recovered A2A Artifacts публикуют их в одинаковой provenance metadata.

## Context и compaction

- [ ] Base tokens считаются как system/kernel/profile + selected tool schemas + output reserve.
- [ ] Working occupancy учитывает только prompt/history/summaries/memory/notifications/artifact excerpts относительно оставшейся working capacity.
- [ ] System prompt и tool schemas не входят в 90%/10–15% threshold.
- [ ] Ниже 90% working occupancy compaction не запускается; при 90% и выше происходит до model call.
- [ ] После compaction working occupancy находится в диапазоне 10–15%.
- [ ] Prompt, policy, task contracts, active constraints, artifact refs и memory provenance остаются pinned.
- [ ] Повторные compactions сохраняют goal и immutable transcript mapping.
- [ ] Непомещающиеся protected/pinned data дают `CONTEXT_UNRECOVERABLE`, не silent truncation.
- [ ] Крупный tool result целиком сохраняется в tenant-scoped artifact и immutable transcript, а active model context получает bounded JSON-ссылку, digest, размер и краткую выдержку вместо полного output.

## Память агента и file lifecycle

- [ ] Память является подсистемой Core Agent: `core_memory_*` являются built-ins, отдельный memory-процесс и MCP-роль `memory` отсутствуют.
- [ ] При `CORE_AGENT_MEMORY=disabled` memory tools/instructions отсутствуют, backend не создаётся и implicit fallback не выполняется; строка `"disabled"` не проходит gating как включённая capability.
- [ ] Markdown corpus является source of truth; BM25/vector/graph indexes полностью перестраиваются из него.
- [ ] Модель не имеет ни filesystem, ни SQL доступа к corpus и меняет его только `core_memory_*` tools.
- [ ] Namespace выводится из scope текущего run: модель передаёт только `user|session`, а `user_id`/`session_id` подставляет runtime.
- [ ] Модель передаёт заголовок и тело, а front matter формирует runtime; корректный YAML от модели не требуется.
- [ ] Перед create/update agent выполняет hybrid search и проверяет top candidates.
- [ ] Та же тема обновляет существующий документ; новый subject/scope создаёт новый.
- [ ] Create/update с 201+ body lines жёстко отклоняется без revision/index changes, truncation или automatic split.
- [ ] `MEMORY_FILE_TOO_LARGE` возвращает actual/max lines и рекомендацию разделить content на несколько документов.
- [ ] Ошибка memory tool возвращается модели как failed tool result и не завершает run: после `MEMORY_FILE_TOO_LARGE` модель вызывает `core_memory_split` в том же run.
- [ ] Отдельный `core_memory_split` принимает явный plan; каждый resulting документ также не превышает 200 lines.
- [ ] Split сохраняет stable IDs/aliases, ссылки и provenance без разрыва semantic block.
- [ ] Update использует expected revision; concurrent conflict не разрешается last-write-wins.
- [ ] Delete исключает content из Markdown, summaries, BM25, vectors, graph и caches.
- [ ] Перевод строки в `title`, `kind`, `status` или элементе `tags` отклоняется как `MEMORY_INVALID`, повторяющийся ключ front matter — тоже, а разобранные `id` и `namespace` сверяются с подставленными runtime-ом для каждого документа, включая дочерние в `split`; ни одна из этих строк не переписывает чужую заметку и не меняет namespace.
- [ ] Определение перевода строки совпадает с парсерным: U+2028, U+2029 и U+0085 отклоняются наравне с `\n`.
- [ ] Элемент `tags` с `,`, `[` или `]` отклоняется, потому что не переживает обратное чтение.
- [ ] Нечитаемая строка хранилища пропускается с warning и не делает недоступным остальной corpus пользователя.
- [ ] Атрибут degraded channels на search span совпадает с `degraded_channels` в tool result, включая деградацию, обнаруженную во время самого поиска.
- [ ] `update` и `split` сохраняют `tags` и `sources` исходного документа.

## Backend памяти и эмбеддинги

- [ ] `MEMORY_STORAGE_TYPE` принимает `in-memory` и `postgres`; доменная семантика лимита, revisions, retrieval и graph одинакова для обоих.
- [ ] Production с включённой памятью и `MEMORY_STORAGE_TYPE=in-memory` завершается ошибкой конфигурации.
- [ ] PostgreSQL backend публикует revision одной транзакцией, переживает рестарт и превращает конфликт номера repository revision в `MEMORY_CONFLICT`.
- [ ] Пул выбирается в порядке `MEMORY_POSTGRES_HOST` → общий пул агента → собственный по `DATABASE_URL`; durable память при `SESSION_STORAGE_TYPE=in-memory` работает, а отсутствие обеих переменных даёт ошибку конфигурации, называющую обе.
- [ ] Пул, открытый самой подсистемой, проходит миграцию или `verify_schema` до старта, а не падает на первом tool call.
- [ ] Conflict возвращает фактическую revision backend-а, подсистема перечитывает состояние до возврата ошибки, и предписанный retry успешно выполняется.
- [ ] `load()` не сочетает новый номер revision со старым corpus: гонка публикации приводит к конфликту, а не к потере чужой записи.
- [ ] Extraction выполняется только для изменённых документов и только моделью агента через `response_format` с полной JSON-схемой; отдельных `MEMORY_NER_*` переменных не существует.
- [ ] Отказ extraction не отменяет запись: документ публикуется с `entities IS NULL`, индексируется встроенным regex-экстрактором и остаётся находимым по BM25, а graph-канал сообщает число неизвлечённых документов без их имён.
- [ ] Сущность, отсутствующая в тексте документа дословно, отбрасывается; offsets вычисляет подсистема, а не модель.
- [ ] Ответ шлюза с кодом 4xx, кроме 408 и 429, выключает extraction до конца жизни процесса, и следующая запись не делает нового запроса к модели.
- [ ] Слой extraction включён только при заданных `LLM_MODEL`, `LLM_API_KEY` и адресе; `LLM_ENDPOINT` заменяет производный адрес, а `LLM_API_FORMAT=anthropic` выключает слой на старте.
- [ ] Сохранённые сущности переживают рестарт: загрузка corpus не выполняет ни одного запроса к модели.
- [ ] Многословная сущность находится по одному слову запроса; graph-канал сопоставляет токены, а не строку целиком.
- [ ] `content` в PostgreSQL хранит Markdown целиком, включая front matter; `kind`, `status`, `sources` и timestamps переживают рестарт без реконструкции.
- [ ] Отсутствие расширения `vector` логируется, понижает векторный канал до вычисления в процессе и не роняет ни миграцию, ни старт.
- [ ] Слой эмбеддингов включается только при заданных `EMBEDDING_MODEL`, `EMBEDDING_API_KEY` и базе; незаданная `EMBEDDING_API_BASE` берётся из `LLM_API_BASE`, а ключ не выводится ниоткуда.
- [ ] `OTEL_ENDPOINT` выводит только `/v1/traces`; metrics и logs уходят исключительно по явным per-signal endpoints, а `ENABLE_OTEL=false` отключает экспорт целиком.
- [ ] Имя сервиса в телеметрии берётся из `OTEL_PROJECT_NAME`, `OTEL_SERVICE_NAME` — синоним с меньшим приоритетом.
- [ ] `ARTIFACT_STORAGE_ENABLED=false` убирает `core_artifact_*` из каталога, не создаёт backend и не требует ни одной другой `ARTIFACT_*` переменной.
- [ ] Любая ошибка embedding endpoint не роняет tool call; текст запроса и ключ не попадают в error, audit и telemetry.
- [ ] Embedding документа вычисляется один раз при публикации и сохраняется; search эмбеддит только запрос.

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
- [ ] Поиск находит заметку на нелатинской письменности без настроенного слоя эмбеддингов: токенизация и извлечение сущностей не зависят от алфавита.

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
- [ ] Child workflow, scheduler handle, task tools, notifications, logs и traces используют один стабильный task ID.
- [ ] Delegation contract содержит узкую instruction, exact tool/MCP/skill allowlists, memory policy и budget; child возвращает обычный text result без artifact tool/schema handoff.
- [ ] Delegation template и model-facing schema всегда требуют оба поля `budget.turns >= 1` и `budget.tool_calls >= 1`; runtime повторно отклоняет missing, zero и boolean до создания child Task.
- [ ] Parent делегирует coherent outcome и minimum sufficient capabilities, а не необязательные mechanical microsteps; runtime предоставляет exactly выбранный capability set.
- [ ] Model-visible delegation prompt/description применяют balanced decision rule: положительные triggers — materially useful parallel work, изоляция большого отделимого context или independently verifiable bounded deliverable; simple/serial/tightly coupled/duplicate/policy-bypass/generic-second-opinion работа явно остаётся у parent.
- [ ] Внутри objective/scope child самостоятельно выбирает strategy, sequencing и delegated tools; procedure фиксируется только для safety/correctness/reproducibility/policy.
- [ ] Child сообщает safe assumptions, но останавливается при выходе за scope, недостающей capability, новом side effect или существенном риске неверного result.
- [ ] Child не видит невыданные рабочие capabilities даже на discovery.
- [ ] Protocol-internal lifecycle/audit остаются enforced, но model-callable child tools равны пересечению EffectiveConfig и delegation allowlist.
- [ ] Child и main разделяют memory только при явной передаче того же Memory MCP server/namespace и tool allowlist.
- [ ] Без переданного Memory MCP child работает без memory.
- [ ] Child memory write проходит service revision/index/NER pipeline и уведомляет parent.
- [ ] Parent воспринимает child text result как недоверенный input; child не общается наружу без capability.
- [ ] Budget-exhausted child возвращает через join/get/wait/notification одинаковый обычный text result с `complete=false`; parent передаёт проверенную часть выше, явно перечисляет unfinished scope и не повторяет выполненное.
- [ ] Depth/fan-out и child budgets ограничены общим parent budget.
- [ ] Cancel root рекурсивно сигнализирует child/grandchild; после ближайшей safe boundary новые model/tools не запускаются, scheduler и workflow имеют terminal `canceled`, а не `failed`.

## Local terminal sessions

- [ ] Main и каждый child получают разные TerminalSession IDs, PTY, process groups и local workspace directories в одном container.
- [ ] Terminal tool не может адресовать session/process другого agent или run.
- [ ] Typed `argv` используется по умолчанию; `cwd`, environment и output bounded и валидируются до запуска.
- [ ] Main и child копируют один immutable base snapshot в разные local directories; merge использует base revision/conflict detection.
- [ ] S3 mount хранит immutable snapshots/checkpoints/artifacts, но active command не выполняется непосредственно на нём.
- [ ] Cancel/timeout завершает owned process group, закрывает PTY и фиксирует cleanup outcome.
- [ ] Secret инжектируется только в environment разрешённого process и не попадает в checkpoint/telemetry/artifact.
- [ ] Runtime явно сообщает logical/process separation и не рекламирует отдельные OS security namespaces.
- [ ] `core_python_exec` доступен в обоих runtime-профилях и управляется только built-in allowlist.
- [ ] Python process получает только bounded `tools.call`; каждый вложенный built-in/MCP вызов повторно проходит EffectiveConfig, schema, policy, общий budget, owner/tenant, audit и OTel.
- [ ] Python exception/nonzero exit/timeout возвращается модели как failed tool result, не завершает родительскую Task и не повторяет неоднозначный side effect.
- [ ] Пакет, установленный командой из `core_terminal_exec`, импортируется в `core_python_exec` без правки `sys.path`; рабочий каталог при этом в `sys.path` не попадает.

## Tools и MCP

- [ ] Tool arguments валидируются до policy и execution.
- [ ] Отказ валидации называет путь аргумента, ожидаемый тип и фактический; переданное значение в сообщение не попадает.
- [ ] Каноническое имя tool не содержит точки, совпадает с именем в каталоге модели и с именем в аргументах `core_delegate`; alias появляется только при коллизии или превышении длины.
- [ ] Имя MCP-тула разрешается в пару `(сервер, tool)` индексом; tool, в имени которого есть точка, вызывается на своём сервере.
- [ ] Schema `core_delegate` перечисляет enum-ом фактический каталог тулов родителя и его skills, а не свободные строки; отдельного аргумента `mcp` нет.
- [ ] Поля `core_delegate` имеют model-facing descriptions; отказ называет отклонённый tool/skill/budget dimension и перечисляет доступный соответствующий набор или limit.
- [ ] Один список `tools` несёт built-ins и MCP-тулы под именами каталога; runtime раскладывает их сам, а отказ называет отклонённое имя и перечисляет доступные.
- [ ] Аргумент со значением `null` обрабатывается как непереданный.
- [ ] Значение `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`, записанное точками до переименования, продолжает называть тот же tool; неизвестное имя отклоняется с перечислением.
- [ ] Shell-синтаксис среди элементов `argv` даёт сообщение про отсутствующий shell, а не ошибку первой попавшейся утилиты.
- [ ] Built-in descriptions кратко и точно отражают фактические ownership/lifecycle ограничения, включая timeout snapshot и запрет task-start для Python/delegate/task/send_message tools; устаревшие config names `core_artifact_put/get` отклоняются.
- [ ] `core_artifact_save` создаёт новую версию и никогда не перезаписывает; `user:`-артефакт виден в другой сессии того же пользователя, а `core_artifact_list` разделяет session и user scope.
- [ ] `core_agent_send_message` ретранслирует прогресс удалённого агента в поток корневой Task, возвращает его финальный текст и пробрасывает downstream только allowlist заголовков.
- [ ] Делегированный child исполняет artifact tools и `core_agent_send_message` вместо `CAPABILITY_DISABLED`, разделяет session scope артефактов с parent-ом и не наследует credentials вызывающей стороны.
- [ ] Непустой `ARTIFACT_S3_ENDPOINT_URL`, отличный от `https://s3.cloud.ru`, не роняет startup, а вызывает warning с проигнорированным и применённым значением; хвостовой `/` отбрасывается без warning.
- [ ] В профиле Cloud.ru итоговый access key без ровно одного `:` или с пустой частью завершает startup `CONFIG_INVALID`; в профиле AWS ключ без `:` принимается.
- [ ] Ни один входящий A2A Part не теряется молча: binary Part при `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS=false` и любой URL-Part отклоняются `CONTENT_TYPE_NOT_SUPPORTED`, а исходящий запрос по caller-адресу не выполняется.
- [ ] При `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS=true` вложение сохраняется артефактом session scope до первого model turn, ведущий `user:` в имени нейтрализуется, а prompt получает строку с именем, версией, media type и размером.
- [ ] MCP tools/resources/prompts/sampling/elicitation проходят local policy независимо от server metadata.
- [ ] MCP-сервер, согласовавший любую поддерживаемую ревизию протокола, подключается; отказ по версии называет предложенную и принимаемые.
- [ ] Выданный сервером `Mcp-Session-Id` возвращается во всех последующих запросах к нему; транспортный отказ называет метод, на котором он произошёл.
- [ ] Ответ выбирается по `id`: нотификации и чужие `id` в event stream пропускаются, и `tools/call` возвращает данные, а не пустой результат.
- [ ] `isError: true` в результате `tools/call` доходит до модели как failed tool result с текстом сервера, а не как успешный пустой output.
- [ ] `MCP_ALLOWED_TOOLS` принимает и голое имя тула, и форму `server.tool`; подключённый сервер без единого разрешённого тула порождает предупреждение с его именем.
- [ ] Production без valid `DATABASE_URL`, ожидаемой PostgreSQL schema или database readiness не открывает A2A listener и не использует in-memory/SQLite fallback.
- [ ] Соединение, закрытое сервером во время простоя, не доходит до caller ошибкой: пул проверяет и заменяет его, а следующий запрос выполняется успешно.
- [ ] Production serving credential не имеет DDL path: `DATABASE_AUTO_MIGRATE=true` отклоняется, а separate migration credential/app role дают только exact DML grants.
- [ ] A2A Tasks, events, checkpoints и audit переживают restart и остаются tenant/owner scoped в одной PostgreSQL transaction boundary.

- [ ] `startup.configuration` описывает runtime mode, built-ins, MCP-серверы с allowlist, удалённых агентов с причинами отказа и storage; `capabilities.resolved` показывает обнаруженные и разрешённые MCP-тулы по серверам. Секреты в обеих записях отсутствуют.
- [ ] `startup.configuration` выводится и тогда, когда логирование настраивает внешний ASGI-сервер после сборки приложения; неподключившийся MCP-сервер получает отдельную запись с кодом ошибки и отличается в `capabilities.resolved` от подключённого с пустым каталогом.
- [ ] `startup.configuration` называет OTLP endpoint для traces, metrics и logs по отдельности и булев признак наличия credentials; значение ключа отсутствует ни в каком виде.
- [ ] Старт печатает инвентарь переменных окружения короткими нумерованными строками `i/N`: те, к которым обращался старт, и отдельно присутствующие, к которым он не обращался, обе группы со состоянием `set|empty|missing`; ни одно значение не выводится.
- [ ] `startup.configuration` перечисляет заданные `REMOTE_AGENTS` отдельно от подключившихся и содержит причину отказа каждого неподключившегося; userinfo из URL удаляется.
- [ ] Помимо `startup.configuration` старт печатает короткую однострочную запись обычным текстом о `REMOTE_AGENTS`: при отсутствии значения — предупреждение о недоступности `core_agent_send_message`, различающее незаданную и заданную пустой переменную и перечисляющее имена присутствующих переменных окружения об агентах без значений, иначе заданные URL без userinfo и имена подключившихся.

## OpenTelemetry

- [ ] Traces, metrics и logs создаются OTel SDK и экспортируются OTLP.
- [ ] W3C Trace Context проходит через A2A, queue, MCP и background/subagent tasks без влияния на authorization.
- [ ] Incoming A2A call создаёт ровно один agent execution trace с root `core_agent.task.execute`; отдельные transport/submission traces отсутствуют, валидный incoming W3C parent продолжается, а независимая background/durable работа использует новый trace со Span Link.
- [ ] Core имеет MCP client span; Memory Service продолжает W3C trace и владеет BM25/vector/graph/rerank/NER spans.
- [ ] OTel semantic-convention version pinned; custom attributes используют `core_agent.*`.
- [ ] Content/arguments/results/system instructions выключены в telemetry по умолчанию.
- [ ] При privileged content capture Phoenix получает visible reasoning в OpenInference `message.contents` с `type=reasoning`; без capture reasoning text отсутствует, но safe reasoning token count сохраняется.
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
2. **Compaction:** working context достигает 90%, сжимается до 10–15%, не считая system/tools, и сохраняет pending Task.
3. **Memory disabled:** Task передаёт optional Memory MCP, AgentConfig его фильтрует, и model не видит memory tools.
4. **Hard line limit:** update на 201+ строк отклоняется без изменений и рекомендует split; следующий явный split создаёт несколько valid Markdown files.
5. **Memory update:** agent находит существующий file через Memory MCP, commit обновляет NER/graph, hybrid search возвращает новую revision.
6. **Shared memory:** parent явно передаёт child тот же Memory MCP namespace; child commit уведомляет parent.
7. **Focused delegation:** child видит только capabilities из EffectiveConfig/delegation allowlist и возвращает обычный text result.
8. **Concurrent work:** main продолжает задачу, пока два child/background Tasks выполняются, затем обрабатывает notifications без busy polling.
9. **Parallel terminals:** main и два child одновременно работают в разных PTY/workspaces, не смешивают output и завершают только свои process groups.
10. **OTel causality:** Core MCP client и Memory Service indexing spans находятся в одном distributed trace без content leakage.
11. **Внешнее действие:** MCP write переживает recovery и выполняется ровно один раз.
13. **Live steering:** пока Task выполняет model/tool loop, два follow-up Messages приходят с тем же `taskId`, сохраняют committed order и учитываются агентом до terminal result без дублирования уже выполненного side effect.

## Definition of Done

- Все критерии и сквозные сценарии проходят в CI, integration, chaos и eval suites.
- A2A schemas и examples проверяются против одного источника типов.
- Recovery tests доказывают отсутствие duplicate side effects и потерянных notifications.
- Memory Service rebuild test удаляет derived indexes и получает эквивалентный searchable graph из Markdown.
- Security review охватывает A2A, model, MCP, skills, memory graph, принятую one-container trust model, subagents, OTel и tenancy.
