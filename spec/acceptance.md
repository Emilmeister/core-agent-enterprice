# Критерии готовности целевого продукта

Поставка может реализовать подмножество только через явный [release profile](releases/v1.md); реализованное поведение не может противоречить этим критериям.

## A2A и публичный контракт

- [ ] Core Agent публикует валидную Agent Card с protocol/binding versions, auth, media types, skills и capabilities.
- [ ] Карточка доступна и по `/.well-known/agent-card.json`, и по историческому `/.well-known/agent.json`; оба пути возвращают идентичный документ, а query-параметры игнорируются.
- [ ] Каждая объявленная в карточке пара binding/version принимается соответствующим endpoint: `JSONRPC` и `HTTP+JSON` отвечают на `1.0`, карточка не содержит пар, которых нет, и не содержит полей карточки A2A 0.3 (`url`, `preferredTransport`, `protocolVersion` верхнего уровня); методы JSON-RPC 0.3 (`message/send` и др.) не принимаются.
- [ ] При заданном `AGENT_URL` карточка объявляет именно его, и никакой заголовок запроса это не меняет; `URL_AGENT` принимается как то же значение, а при обоих заданных выигрывает `AGENT_URL`.
- [ ] Без `AGENT_URL` карточка объявляет адрес из `X-Forwarded-Proto`/`X-Forwarded-Host`, иначе из схемы и `Host`; непригодное значение отбрасывается, а запрос карточки всё равно завершается успешно.
- [ ] Лишний член верхнего уровня JSON-RPC конверта не отклоняет запрос, не интерпретируется как session id и попадает в предупреждение с именами удалённых полей.
- [ ] Внешнее общение использует A2A Message/Task/Artifact; отдельная публичная task state machine отсутствует.
- [ ] Message Parts отображаются на `prompt`; собственное расширение протокола не требуется и не объявляется в Agent Card.
- [ ] Неподдерживаемая required extension/version возвращает стандартно отображаемую A2A ошибку до model turn.
- [ ] A2A `contextId` сохраняет session, но MCP/skills не наследуются в новую Task неявно.
- [ ] Blocking, `return_immediately`, polling, subscription/streaming и push видят одну durable Task history.
- [ ] Disconnect stream не отменяет Task; at-least-once push update дедуплицируется.
- [ ] Fresh subscription после process restart сначала отдаёт persisted Task, пассивно наблюдает durable updates и не resume-ит `WAITING_*`/`PAUSED`/`auth-required`; terminal update остаётся последним.
- [ ] Подписка на process-local active Task отдаёт ранее прочитанный persisted snapshot до live events и закрывается на первом terminal event без последующих artifact/status frames.
- [ ] Recovery terminalizes A2A Task и ставит push delivery одной транзакцией; ошибка enqueue откатывает обе записи, а повторная сверка создаёт ровно одну delivery. Восстановленный `failed`/`rejected` содержит безопасный stable error code и не раскрывает raw error.
- [ ] `LEASE_LOST` и graceful shutdown после durable admission не terminalize-ят A2A Task; shutdown до admission даёт безопасный `failed`, а не вечный `working`. Поздний `CancelTask` не превращает уже terminal workflow в `failed`, а отражает сохранённый итог локального producer или durable recovery. Cancel чужой/idle Task не оставляет process-local coordination entry.
- [ ] Follow-up Message с существующим non-terminal `taskId` возвращает ту же Task, durable переживает restart и попадает отдельным user turn перед следующим model call.
- [ ] Если `failed`/`canceled` выигрывает после принятого follow-up, terminal gate переносит Message в transcript с причиной `unprocessed_due_to_failure`/`unprocessed_due_to_cancel`, не вызывает модель или tool и не оставляет unread inbox.
- [ ] Duplicate `messageId` не доставляется дважды; concurrent Messages получают стабильный committed order; context/task mismatch и cross-tenant ID отклоняются.
- [ ] Уже начатый model/tool/side-effect call не прерывается; completion atomically проигрывает более раннему accepted Message, поэтому подтверждённый input не теряется.
- [ ] Follow-up не изменяет EffectiveConfig/MCP/skills/budgets.
- [ ] Internal states корректно отображаются только на стандартные A2A Task states.

## Конфигурация агента

- [ ] При shared PostgreSQL startup/recovery приложения компании B исполняет
  только работу B; root/background/remote/cancel/wait/cleanup/projection/push
  записи A не меняются и не используют модель либо credentials B. Более одного
  batch чужих rows не блокирует B; generic runtime без tenant scope сохраняет
  прежнюю platform-wide semantics, неизвестные rowless files не удаляются.
- [ ] PlatformConfig, AgentConfig и Task input разделены; Task не расширяет AgentConfig.
- [ ] AgentConfig может отключить memory, terminal, filesystem mutations, background tasks, delegation, отдельные built-in/MCP tools и skills.
- [ ] Disabled tool отсутствует в discovery/model context и stale call получает `CAPABILITY_DISABLED`.
- [ ] Memory modes `disabled`, `optional`, `required` корректно фильтруют/требуют встроенный memory backend.
- [ ] MCP tool filters применяются после discovery, но до model context; deny имеет приоритет.
- [ ] Admission ceiling и config/tool snapshot immutable внутри Task; текущая owner policy применяется к последующим calls, все использованные versions/digests сохраняются в audit.
- [ ] Agent Card не рекламирует capability, отключённую AgentConfig или текущим
  owner deny. После owner policy update оба A2A входа и оба well-known пути
  отражают deny/allow/HITL без restart; policy другой компании не влияет на Card.
- [ ] Присутствующая, но пустая deployment-переменная трактуется как отсутствующая
  и получает документированный default, кроме явно документированных allowlist:
  пустой `CORE_AGENT_ALLOWED_SKILLS` отключает навыки.
- [ ] Невалидная конфигурация не поднимает listener, завершает процесс ненулевым кодом и печатает ровно одну строку с именем настройки и стабильным кодом, без traceback и без значений secret-переменных.
- [ ] Неразбираемые числовые deployment values, `NaN`/infinite floats, неверный `LOG_LEVEL`, неизвестные `TASK_STORAGE_TYPE`/`A2A_CAPABILITIES` и нечисловые HTTP retry codes отклоняются с `CONFIG_INVALID`, именем настройки и без введённого значения; пустые значения сохраняют documented defaults, listener не запускается. Ошибка старта остаётся видимой при `LOG_LEVEL=CRITICAL/FATAL`.

## Kernel instructions

- [ ] KernelInstructions всегда присутствуют отдельно от AgentProfilePrompt и имеют version/digest в audit.
- [ ] Пустой AgentProfilePrompt не добавляет generic system instruction; фактический prompt присутствует только как отдельный user Message/context item.
- [ ] Base kernel выбирает tool по смыслу задачи, не требует tool без материальной пользы и не дублирует conditional capability/tool-description semantics.
- [ ] AgentProfilePrompt, user Message, skill или MCP output не могут изменить rules включённой capability; отключать optional capability может только config/policy.
- [ ] До каждого model turn контекст содержит имена и краткие descriptions всех и
  только skills из EffectiveConfig, но не содержит тела неактивированных
  `SKILL.md`, package paths или resources.
- [ ] Модель может выбрать skill по смыслу запроса без буквального имени и без
  slash-команды; `core_skill_activate` принимает enum effective names, закрепляет
  digest идемпотентно и добавляет полные инструкции только со следующего turn.
- [ ] `features.skills=false` или отсутствие platform feature дают пустой
  effective skill catalog и скрывают оба служебных tools независимо от allowlist.
- [ ] После запроса `core_skill_activate` оставшиеся calls того же assistant
  response не dispatch-ятся, получают `SKILL_ACTIVATION_BOUNDARY` и могут быть
  выбраны заново только следующим model turn с полным `SKILL.md`.
- [ ] Имена обоих служебных skill tools зарезервированы от MCP collision, а
  response/reasoning deltas удерживаются до классификации ответа и отбрасываются,
  если ответ запрашивает активацию.
- [ ] `core_skill_read_resource` существует только для активного skill, принимает
  enum `<skill>/<relative-path>`, читает bounded UTF-8 content из immutable
  snapshot и отклоняет неизвестный, бинарный, symlink, absolute или escaping path.
- [ ] Активация и чтение resource расходуют общий tool-call budget, проходят audit
  и OTel и возвращают recoverable structured failure; child получает эти
  служебные tools только из явно делегированного skills allowlist, без отдельных
  записей в `core_delegate.tools`.
- [ ] Runtime принимает при новом admission только закреплённые digest `SKILL.md`
  и полный manifest ресурсов и отклоняет подмену после activation. Legacy active
  skill продолжает сохранённые instructions без чтения live package, новых
  activations и ресурсов. После activation base context budget пересчитывается до
  следующего model call.
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
- [ ] Root recovery filter применяется до batch limit, поэтому любое число child workflow не блокирует восстановление root; graceful worker shutdown останавливает active и ожидающие continuation без terminal transition.
- [ ] Проигравшая workflow lease recovery-attempt завершается после одного claim без 50-миллисекундного polling; повтор допускается только следующим coordinator scan.
- [ ] Durable `cancel_requested` выигрывает у более позднего success/start failure и даёт `CANCELLED`; только неоднозначный `EXECUTING` outcome сохраняет `ABORTED`/`SIDE_EFFECT_UNKNOWN`.
- [ ] Workflow transition повторно проверяет lease token/expiry на финальном `core_runs` update после ожидания shared budget или других transaction locks; lock wait через TTL даёт `LEASE_LOST` и откатывает весь transition/charge.
- [ ] Mutating exception после committed intent, resume/recovery или cancel workflow в `EXECUTING` дают `SIDE_EFFECT_UNKNOWN` без redispatch и без ложного `CANCELLED`, включая scheduler notification.
- [ ] При restart `cancel_requested` recoverable Task согласует durable child state один раз, а non-recoverable mutating Task получает reconciliation error вместо ложного `canceled` outcome; cancel между scan и claim либо сразу после claim не обходит reconciliation и не стирает её ошибку.
- [ ] Cancelled child атомарно возвращает неиспользованный finalization reserve; начатый finalizer остаётся учтённым как provider call.
- [ ] Budget finalization ждёт owned Tasks только bounded grace; некооперативная Task остаётся durable/cancel-requested, а partial result возвращается и перечисляет её в `pending_tasks` без выдуманного outcome.
- [ ] Persisted result различает local `usage` и общий `shared_budget`; live и crash-recovered A2A Artifacts публикуют их в одинаковой provenance metadata.

## Context и compaction

- [ ] Owner `/api/chats` показывает общий company список, включая чаты внешних
  callers, с bounded pagination и актуальным active root; external/dual-role
  получает 403, другая company не видит записи, чтение не вызывает модель.
- [ ] Base tokens считаются как system/kernel/profile + selected tool schemas + output reserve.
- [ ] Working occupancy учитывает только prompt/history/summaries/memory/notifications/artifact excerpts относительно оставшейся working capacity.
- [ ] System prompt и tool schemas не входят в 90%/10–15% threshold.
- [ ] Ниже 90% working occupancy compaction не запускается; при 90% и выше происходит до model call.
- [ ] После compaction working occupancy находится в диапазоне 10–15%.
- [ ] Prompt, policy, task contracts, active constraints, artifact refs и memory provenance остаются pinned.
- [ ] Повторные compactions сохраняют goal и immutable transcript mapping.
- [ ] Public UI bootstrap возвращает только configured issuer/public client ID,
  без server secret, introspection client и identity. Неверная/неполная browser
  configuration отклоняется при startup; bootstrap query не меняет issuer.
  Owner API/A2A остаются авторизованными, выключенный bootstrap недоступен.
- [ ] Owner registry CRUD имеет company scope, immutable name/revisions и CAS;
  secret omission сохраняет credential, null очищает, disabled revision не удаляет
  pinned старую. Header values отсутствуют в GET/errors/audit/model; ciphertext
  связан с tenant/peer/revision/header и не переносится между адресатами.
- [ ] Remote timeout/poll defaults86400/300 сохраняются старым settings PUT;
  операция закрепляет settings и peer revision. Deadline начинается до первого
  разрешённого Send; повтор remote wait не продлевает его и не принимает timeout
  override. Late result и cancel после timeout не открывают network/continuation.
- [ ] Новая root Task атомарно закрепляет previous root того же canonical чата;
  owner continuation external чата сохраняет execution owner. Metadata не меняет
  эту ссылку; duplicate и busy Task не вклиниваются в цепочку, а другая company
  или чат не становятся источником истории.
- [ ] Вторая и третья Task одного чата получают проверенные прежние решения и
  итог с original provenance; новая инструкция pinned, прежние цели historical.
  Recovery не дублирует импорт, старые tool calls/replay не отправляются provider.
  Foreign/child/nonterminal/missing источник отклонён. Отзыв материала исключает
  зависимые прошлые summary и final result; failed/partial outcome явно сохранён.
- [ ] Semantic summary сохраняет поздние исправления и различает факт, вывод,
  предположение и план; malformed/truncated/unknown-source ответ не заменяет
  исходный context. Из active provider context не исчезает половина tool-call/result пары.
- [ ] Compaction attempt заранее оплачена из root model budget; restart между
  attempt и commit не сбрасывает максимум двух попыток и не меняет source boundary.
- [ ] Ошибка interval compaction ниже pressure сохраняет прежний context;
  `LEASE_LOST` и storage ошибки по-прежнему прерывают исполнение.
- [ ] Материал, сначала допущенный и включённый в summary, после canonical
  отказа по тем же данным исключается вместе с зависящим summary из следующего
  model call и истории новой Task; полный private audit transcript остаётся.
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
- [ ] Child и main разделяют memory только при явной передаче того же memory scope и явного `core_memory_*` allowlist.
- [ ] Без переданных memory tools child работает без memory.
- [ ] Child memory write проходит service revision/index/NER pipeline и уведомляет parent.
- [ ] Parent воспринимает child text result как недоверенный input; child не общается наружу без capability.
- [ ] Budget-exhausted child возвращает через join/get/wait/notification одинаковый обычный text result с `complete=false`; parent передаёт проверенную часть выше, явно перечисляет unfinished scope и не повторяет выполненное.
- [ ] Depth/fan-out и child budgets ограничены общим parent budget.
- [ ] Cancel root рекурсивно сигнализирует child/grandchild; после ближайшей safe boundary новые model/tools не запускаются, scheduler и workflow имеют terminal `canceled`, а не `failed`.

## Local terminal sessions

- [ ] Main и каждый child получают разные TerminalSession IDs, PTY и process groups в границах постоянного workspace своего чата; завершение child сохраняет файлы чата.
- [ ] Terminal tool не может адресовать session/process другого agent или run.
- [ ] Typed `argv` используется по умолчанию; `cwd`, environment и output bounded и валидируются до запуска.
- [ ] Если child использует отдельную scratch-копию для изоляции изменений, она создаётся из проверенного immutable base snapshot, а merge использует base revision/conflict detection. Без scratch-копии пересекающиеся записи parent/child явно координируются.
- [ ] S3 mount хранит immutable snapshots/checkpoints/artifacts, но active command не выполняется непосредственно на нём.
- [ ] Cancel/timeout завершает owned process group, закрывает PTY и фиксирует cleanup outcome.
- [ ] Secret инжектируется только в environment разрешённого process и не попадает в checkpoint/telemetry/artifact.
- [ ] Runtime применяет обязательные Bubblewrap namespaces и egress, без fallback при недоступном профиле; факт проверки целевого кластера подтверждён отдельно.
- [ ] `core_python_exec` доступен в обоих runtime-профилях и управляется только built-in allowlist.
- [ ] Python process получает только bounded `tools.call`; каждый вложенный built-in/MCP вызов повторно проходит admission ceiling, schema, current owner policy/HITL, общий budget, owner/tenant, audit и OTel.
- [ ] Python exception/nonzero exit/timeout возвращается модели как failed tool result, не завершает родительскую Task и не повторяет неоднозначный side effect.
- [ ] Пакет, установленный командой из `core_terminal_exec`, импортируется в `core_python_exec` без правки `sys.path`; рабочий каталог при этом в `sys.path` не попадает.
- [ ] Образ v1 проходит реальную проверку основного набора CLI, включая Mike Farah `yq` и PDF-команды; краткое описание `core_terminal_exec` называет набор, явно не считает его allowlist и разрешает дополнительные workspace-инструменты только при допустимых policy и сети.

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
- [ ] `core_agent_send_message` ретранслирует прогресс удалённого агента в поток корневой Task, возвращает durable handle; результат доставляется через wait, downstream использует только secret-header настройки адресата.
- [ ] Делегированный child использует выданные file/remote capabilities с тем же tenant/caller/chat scope и не получает credentials вызывающей стороны.
- [ ] Ни один входящий A2A Part не теряется молча: file Parts атомарно сохраняются по FILE-02; unsupported references отклоняются, произвольный caller URL не даёт SSRF или credential forwarding.
- [ ] MCP tools/resources/prompts/sampling/elicitation проходят local policy независимо от server metadata.
- [ ] MCP-сервер, согласовавший любую поддерживаемую ревизию протокола, подключается; отказ по версии называет предложенную и принимаемые.
- [ ] Выданный сервером `Mcp-Session-Id` возвращается во всех последующих запросах к нему; транспортный отказ называет метод, на котором он произошёл.
- [ ] `MCP_HEADERS_JSON` с любым регистром не переопределяет transport-managed заголовки session, protocol, method, name, content type или accept и отклоняется при startup как `CONFIG_INVALID`; все MCP timeout принимают только конечные значения допустимого знака.
- [ ] Новый запрос переживает scale-to-zero MCP: workflow и admission capability ceiling видны до сети, один durable deadline ограничивает discovery всех серверов и recovery без расхода model/tool budget, session изолирован по run, stale session очищается, отмена прерывает ожидание, а permanent auth/protocol error не повторяется. После deadline optional server исключается с `MCP_CONNECTION_FAILED`, но Task продолжает работу. Recovery после успешного discovery использует сохранённый catalog и отдельный durable reconnect deadline, не меняет EffectiveConfig, автоматически повторно подхватывает безопасное состояние после истечения старого lease без клиентского resubscribe и не оставляет повторяемый `CHECKPOINT_INVALID`; неразрешённое passive wait не запускается; resolved wait возобновляется один раз. Cancel до workflow admission не теряется по локальному timeout, а cross-worker cancel сначала durable сохраняет intent и получает terminal `canceled` только от fenced lease-owner/recovery. Read-only transport failure возвращается модели, неизвестный outcome mutating `tools/call` не повторяется. Инструмент считается изменяющим по умолчанию независимо от имени и server annotations; только явное совпадение с доверенным `MCP_READ_ONLY_TOOLS` разрешает обработать неизвестный transport outcome как ошибку только для чтения.
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
- [ ] `startup.configuration` перечисляет configured owner registry отдельно от подключившихся и содержит причину отказа каждого неподключившегося; userinfo из URL удаляется.

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
3. **Memory disabled:** AgentConfig отключает встроенную память, и model не видит memory tools.
4. **Hard line limit:** update на 201+ строк отклоняется без изменений и рекомендует split; следующий явный split создаёт несколько valid Markdown files.
5. **Memory update:** agent находит существующий file через `core_memory_search`, commit обновляет NER/graph, hybrid search возвращает новую revision.
6. **Shared memory:** parent явно передаёт child тот же memory scope и явный tool allowlist; child commit уведомляет parent.
7. **Focused delegation:** child видит только capabilities из EffectiveConfig/delegation allowlist и возвращает обычный text result.
8. **Concurrent work:** main продолжает задачу, пока два child/background Tasks выполняются, затем обрабатывает notifications без busy polling.
9. **Parallel terminals:** main и два child одновременно работают в разных PTY одного чата, не смешивают output и завершают только свои process groups; общий workspace сохраняется, пересекающиеся изменения координируются либо изолируются в scratch-копиях с проверяемым merge.
10. **OTel causality:** Core MCP client и Memory Service indexing spans находятся в одном distributed trace без content leakage.
11. **Внешнее действие:** MCP write переживает recovery и выполняется ровно один раз.
13. **Live steering:** пока Task выполняет model/tool loop, два follow-up Messages приходят с тем же `taskId`, сохраняют committed order и учитываются агентом до terminal result без дублирования уже выполненного side effect.

## Definition of Done

- Все критерии и сквозные сценарии проходят в CI, integration, chaos и eval suites.
- A2A schemas и examples проверяются против одного источника типов.
- Recovery tests доказывают отсутствие duplicate side effects и потерянных notifications.
- Memory Service rebuild test удаляет derived indexes и получает эквивалентный searchable graph из Markdown.
- Security review охватывает A2A, model, MCP, skills, memory graph, Bubblewrap/egress границу одного Kubernetes Pod, subagents, OTel и tenancy.

## Enterprise migration и transport trust boundary

- [ ] Корпусы памяти с одинаковыми agent/owner ID разных trusted tenants изолированы в memory и PostgreSQL после restart; child получает только делегированный исходный scope. Known session-A ID недоступен read/update/split/delete из session-B или user namespace; отказ не меняет ни contents, ни revision. Migration23→24 сохраняет legacy rows под пустым tenant без автоматического присвоения; новые corpus не читают их, old image fail closed на новой schema.
- [ ] SDK/path/JSON-RPC tenant не перезаписывает trusted deployment company: mismatch отклоняется, пустое SDK значение сохраняет verified tenant.
- [ ] Legacy identity явно сопоставляется issuer/sub; неизвестный owner остаётся изолированным, исходные rows/blobs и digests сохраняются при migration.
- [ ] Versioned wait/cron/file migration сохраняет deadlines, closed outcomes и visibility; unknown dispatched side effects не replay-ятся.
- [ ] Перенос входящих files предшествует удалению artifact tools; прежних named artifact данных для переноса нет, отдельный export/import utility не поставляется. Три tools и исключительно их adapters/dependencies/config удалены из приложения без удаления внешних storage resources; устаревшие tool/env настройки диагностируются без вывода values. Owner registry import не перезаписывается ENV после cutover; rollback после enterprise writes требует безопасного reverse migration/backup boundary.

- [ ] Explicit remote registry import использует bounded version1 operator file,
  deployment company и database-authenticated migration actor; все encrypted peers
  коммитятся одним batch без discovery/forwarding. Повтор или nonempty registry
  отклоняются без перезаписи owner edits; invalid/key/duplicate-name/DB failure
  не оставляет pointers или revisions. Другой tenant и legacy Task/chat ownership
  не меняются; serving restart/ENV не запускают import. DB error не раскрывает
  values и возвращает `REMOTE_IMPORT_FAILED`.

## Enterprise v1: обязательные сценарии

Все ENT-AC ниже входят в текущий release candidate. Перенос критерия не является автоматическим доказательством: статус исполнения фиксируется отдельно в implementation-status.

| ID | Дано / действие | Наблюдаемый результат |
| --- | --- | --- |
| ENT-AC-01 | Два владельца с нужной ролью входят в UI | Оба видят общие чаты и могут принимать разрешённые решения |
| ENT-AC-02 | Внешний агент отправляет решение HITL | Доступ отклонён, состояние approval и вызова не меняется |
| ENT-AC-03 | Агент B использует task/context/file ID агента A, в том числе в list/stream | Данные и существование чужих объектов не раскрываются; собственные доступны |
| ENT-AC-04 | Токен отозван, истёк либо introspection недоступна | Новый защищённый запрос не получает доступ |
| ENT-AC-05 | Токен сервисной учётной записи заменён | Новый валидный токен видит прежние собственные задачи |
| ENT-AC-06 | Инструмент запрещён через UI | Он отсутствует в следующем каталоге; прямой, nested Python и delegated вызовы блокируются |
| ENT-AC-07 | Подключён новый разрешаемый platform-ой инструмент | Он требует HITL до выполнения первого вызова |
| ENT-AC-08 | Владелец разрешает конкретный вызов | Выполняется согласованный вызов с исходными аргументами, без повторного dispatch из-за дубля решения |
| ENT-AC-09 | Отказ либо 24 часа без решения HITL | Side effect не выполняется; модель получает результат и может продолжить |
| ENT-AC-10 | Сервис перезапущен во время HITL | Запрос и прежний deadline восстановлены, повторного вызова нет |
| ENT-AC-11 | Через UI и A2A приходят вложения в сумме на лимите и на байт больше; владельцы меняют общий лимит через settings CAS | Первый набор доступен в правильной папке после проверки; превышение отклонено; scope сохранён. Default 25 000 000 bytes; external не меняет настройку, старый timeout-only PUT сохраняет лимит, уже принятые manifest не переоцениваются |
| ENT-AC-12 | Имя файла содержит traversal либо путь ведёт через symlink к чужой папке | Чужие данные не читаются и не перезаписываются |
| ENT-AC-13 | Агент выдаёт файл через UI/A2A | Авторизованный получатель получает файл; другой внешний агент доступа не получает |
| ENT-AC-14 | Два одновременных запроса создают задачи в одном свободном чате | Выполняется одна; другая сохранена и возвращена как `failed` с `CONTEXT_BUSY` |
| ENT-AC-15 | Новая задача поступает в чат с ожидающей HITL/remote задачей | Новая попытка `failed`; старая задача и её ожидание продолжаются |
| ENT-AC-16 | Follow-up приходит во время одного tool call | Вызов завершается; перед следующим model call присутствуют результат и затем новое сообщение |
| ENT-AC-17 | Один follow-up доставлен повторно либо попал в гонку с завершением | Нет двойной доставки или молчаливой потери принятого сообщения |
| ENT-AC-17a | Follow-up приходит во время HITL/remote ожидания | Сообщение сохранено, model call не запускается; после завершения ожидания сообщение присутствует перед следующим model call |
| ENT-AC-18 | Remote Task долго ждёт своего пользователя, stream закрывается и наш сервис перезапускается | Родитель остаётся нетерминальным; ожидание восстанавливается без повторной отправки задания |
| ENT-AC-19 | Срок remote wait истёк, затем тот же wait вызван снова, наступил очередной polling tick или произошёл restart | Немедленный окончательный timeout, прежний deadline не продлевается; новые запросы результата и повторные подключения не выполняются, CancelTask не отправлен |
| ENT-AC-20 | Remote completion и timeout происходят одновременно | Зафиксирован один исход ожидания и одно продолжение |
| ENT-AC-21 | Наступило сохранённое время пробуждения после restart | Продолжение запускается для проверки события, событие не объявляется свершившимся автоматически |
| ENT-AC-21a | Timer или local task wait сохранён; worker/сервис перезапущен до либо после resolve | Workflow lease, scheduler claim и execution thread освобождены; outcome применяется один раз к исходному call без повторного model/tool charge; новый follow-up будит только timer, duplicate не будит следующую generation |
| ENT-AC-21b | Joined child приостановлен; родитель перезапущен либо отменён | Child admission и parent wait атомарны; восстановление использует прежний child ID, required child остаётся nonterminal во время ожидания; cancel закрывает family waits и не теряет принятый inbox |
| ENT-AC-21c | Старый SDK Task snapshot или очередной Artifact chunk приходит после более нового workflow transition | Canonical state не понижается, private устаревший status message не возвращается; SDK chunk aggregate не изменяется reconciliation-ом и финальный Artifact не дублируется |
| ENT-AC-22 | Наступил cron tick при активном предыдущем запуске | Нет нового выполнения и очереди; в чате виден immutable пропуск со stable cursor, включая пустой чат до первой Task; чтение не запускает модель |
| ENT-AC-23 | Инструмент создания cron запрещён | Модель его не видит; владелец всё ещё управляет расписаниями через UI |
| ENT-AC-24 | Следующий cron-запуск начинается после нескольких compaction | Модель получает актуальные решения и подтверждённые результаты прежних запусков |
| ENT-AC-25 | Позднее уточнение отменяет раннее решение; история суммаризируется несколько раз | Актуально позднее решение, план не превращён в выполненное действие |
| ENT-AC-26 | Summary потеряло упоминание запрета/закрытого ожидания | Структурированная policy/state всё равно блокирует действие или повторный wait |
| ENT-AC-27 | Guardrail передаёт подозрительный материал владельцу | До решения материал не используется для защищаемого продолжения; одобрение не расширяет права |
| ENT-AC-28 | Artifact tools и dedicated backend отключены/удалены | Их нет в catalog/config/dependencies; входящие файлы и обычные A2A outputs продолжают работать |
| ENT-AC-29 | Команды двух чатов одновременно работают через Bubblewrap | Каждая читает и изменяет свой `/workspace`; содержимое соседнего чата недоступно |
| ENT-AC-30 | Python, shell и их потомки пробуют чужие пути, `/proc`, сокеты и platform secrets | Ограничения сохраняются для всех процессов; чужой broker и данные недоступны; собственный разрешённый tool call работает |
| ENT-AC-31 | Bubblewrap отсутствует либо кластер запрещает обязательную изоляцию | Недоверенная команда не запускается; возвращается ошибка окружения без fallback к обычному процессу |
| ENT-AC-32 | После записи файла окружение исполнения и pod пересозданы с тем же постоянным томом; запускаются root, child и background/recovered команды | Тот же authenticated chat binding получает прежний файл в `/workspace`; соседний чат его не видит; owner, работающий в external чате, сохраняет его binding. Непривязанный legacy run отклоняется, старый base snapshot не перезаписывает постоянный чат |
| ENT-AC-33 | В sandbox выполняются requests/curl и установка пакета из публичного источника при разрешённой tool policy | Сеть работает, пакет записывается только в разрешённую область чата |
| ENT-AC-34 | Процесс обращается к внутреннему IP, metadata endpoint, серверному localhost, соседнему окружению либо перенаправляется туда с публичного URL | Доступ запрещён, в том числе при прямом socket-соединении, без proxy env и при изменении DNS-ответа |
| ENT-AC-35 | Используются IPv4, IPv6 и альтернативные записи адресов при работающих серверных интеграциях | Сетевая граница не обходится; сервер сохраняет свой разрешённый доступ к БД/Keycloak/MCP/A2A |
| ENT-AC-36 | Контроль выхода sandbox в сеть не удалось запустить или он отказал | Неконтролируемый доступ к сети pod не появляется |
| ENT-AC-37 | Владелец отклоняет подозрительное сообщение, файл или tool result | Модель получает уведомление об отказе без содержимого; задача может продолжиться по разрешённым данным и автоматически не становится `failed` |
| ENT-AC-38 | После отказа агент пытается получить материал через summary/retrieval/memory, чтение файла либо другой инструмент | Отклонённое содержимое не выдаётся; разрешённые материалы того же чата остаются доступны. Поздний отказ по точному материалу до publication исключает ранее одобренный batch, включая recovery после rename до DB commit; private owner history сохраняет bytes и оба решения, повторное одобрение не раскрывает batch |
| ENT-AC-39 | Владелец не решил запрос guardrails до настроенного deadline; для настройки по умолчанию прошло 24 часа | Материал остаётся закрытым; модель получает уведомление о timeout и может продолжить; задача автоматически не становится `failed` |
| ENT-AC-40 | Во время ожидания guardrails сервис перезапущен либо решение владельца совпало с deadline | Срок не начинается заново; фиксируются один исход и одно продолжение, follow-up не теряются |
| ENT-AC-41 | Требуемая проверка guardrails завершилась ошибкой или детектор недоступен | Материал закрыт; владелец получает запрос «Не удалось проверить»; отказ и timeout обрабатываются по GUARD-02/03 |
| ENT-AC-42 | Владелец помечает инструмент как доверенный для guardrails | Детектор не вызывается для аргументов и результатов этого инструмента; tool policy, HITL, schema validation и изоляция продолжают действовать |
| ENT-AC-43 | Новый MCP tool или внешний caller объявляет себя безопасным через metadata/текст | Исключение не включается; доверенную настройку может изменить только владелец |
| ENT-AC-44 | Детектор недоступен, а для конкретного инструмента действует исключение TOOL-02 | Для исключённой области нет вызова детектора и запроса guardrails из-за его ошибки; необходимость HITL определяется независимо |
| ENT-AC-45 | Проверяется сформированный запрос к настраиваемой модели guardrails и подставляется заданный вердикт | У детектора отдельный контекст без tools, секретов и чужой истории; результат обрабатывается по GUARD-01–04. Нет clear при дополнительных/повторяющихся JSON-полях, tool calls, отсутствующем признаке завершения, непроверенном хвосте или исчерпанном budget/deadline; попытка сохраняется до сети, поздний clear игнорируется. Точность классификации на данных не является критерием |
| ENT-AC-46 | Один вызов ждёт HITL; владелец включает автоматический режим, затем поступает новый вызов в другом чате | Старый запрос ждёт решения с прежними аргументами/deadline; новый не требует tool HITL, остальные проверки сохраняются |
| ENT-AC-47 | Во время задачи внешнего агента наш агент задаёт владельцам вопрос и получает ответ | Владельцы видят переписку в чате; внешний caller видит факт ожидания и итоговый ответ, но не внутреннюю переписку через историю, GetTask/ListTasks, stream, push, файлы, артефакты или metadata, в том числе после восстановления |
| ENT-AC-48 | Внешний caller присылает follow-up, пока ожидается ответ владельца | Сообщение сохраняется по правилам follow-up, но не считается ответом владельца и не закрывает ожидание |
| ENT-AC-49 | Владельцы не отвечают на уточняющий вопрос до настроенного срока, в том числе после restart; ответ может конкурировать с timeout | По умолчанию срок равен 24 часам и не сбрасывается; при timeout агент получает «ответ не получен вовремя» и может продолжить без этих сведений, без автоматического failed; завершение ожидания и возобновление однократны |
| ENT-AC-50 | После зафиксированного remote timeout приходит ответ на ранее отправленный запрос или SSE/push-событие, если этот канал используется | Результат игнорируется, не публикуется в чате и не передаётся модели; исход ожидания не меняется, повторного продолжения нет |
| ENT-AC-51 | В нескольких чатах вызовы одного инструмента ждут HITL; владелец запрещает инструмент, затем приходят поздние разрешения либо происходит restart | Все ожидавшие вызовы закрыты без исполнения с результатом «инструмент запрещён»; задачи могут продолжиться, закрытые запросы не восстанавливаются и повторного продолжения нет |
| ENT-AC-52 | Сервис недоступен в один или несколько моментов cron-расписания, затем восстанавливается | Пропущенные запуски не выполняются, включая один компенсирующий; scheduler ждёт следующего момента расписания, в чате виден пропуск. Уже принятая до сбоя задача восстанавливается по общим правилам, без повторного создания. Startup/leader смена/gap >60 секунд фиксируют cutoff; healthy lateness >60 секунд также пропускается. Crash между admission и event/next_due не оставляет частичный commit; duplicate/unknown COMMIT и два coordinators не создают вторую Task |
| ENT-AC-53 | Владелец отключает или удаляет расписание с активной задачей, в том числе ожидающей HITL; затем наступает cron tick или происходит restart | Новые задачи по расписанию не создаются; принятая задача продолжает работу или восстанавливается, её чат, файлы и HITL доступны; отмена задачи требует отдельного действия |
| ENT-AC-54 | Создаются расписания с поясом по умолчанию и явно выбранным поясом; сервис перезапускается с другим системным timezone | Default равен Europe/Moscow, явный выбор сохраняется; моменты запусков определяются поясом каждого расписания, UI показывает его и ближайший запуск; неверный идентификатор отклоняется Диалект содержит пять полей и поддерживает names/lists/ranges/positive steps, Sunday 0/7 и DOM/DOW OR; invalid/impossible выражения отклоняются безопасно до сохранения. Gap пропускается, fold выполняется только в первом occurrence; Berlin/Lord_Howe и строго следующий UTC instant проверяются на реальном закреплённом parser release. |
| ENT-AC-55 | Владелец нажимает «Запустить сейчас» в свободном чате расписания | Создаётся обычная задача с сохранённым prompt в том же чате и workspace; расписание и время следующего cron-запуска не сдвигаются; действуют обычные HITL и guardrails. Owner API и tool имеют idempotent receipts/CAS, foreign403/404 и strict validation; create без context создаёт пустой чат, tool использует текущий чат без выбора identity. Retry run-now после edit/disable/delete возвращает прежнюю Task; late fenced tool completion не создаёт schedule |
| ENT-AC-56 | В чате уже есть активная задача либо ручной запуск конкурирует с другим стартом | UI блокирует кнопку при известной занятости; сервер атомарно допускает не более одной активной задачи, проигравшая ручная попытка получает failed/CONTEXT_BUSY без очереди; cron tick при активном ручном запуске пропускается |
| ENT-AC-57 | Во время активной задачи владелец меняет prompt или время расписания; задача выходит из ожидания либо восстанавливается после restart | Текущая задача сохраняет исходное задание; будущие автоматические и ручные запуски используют новые параметры; при конкурентном приёме сохраняется одна согласованная версия, редактирование само не запускает задачу |
| ENT-AC-58 | Владелец открывает очистку workspace, выбирает файлы и подтверждает удаление | До подтверждения видны имена, пути, размеры, даты, выбранный перечень и общий объём; preview/download проверяют owner/company и original chat binding, не запускают модель или workflow и не создают отсутствующий workspace. Cursor связан с каталогом/фильтром/server timestamp; unsafe files/control manifests не выдаются, traversal limit даёт явную ошибку. Удаляются только выбранные неизменившиеся файлы; UI показывает удалённые, пропущенные и ошибки, чат и история сохраняются |
| ENT-AC-59 | Между просмотром и удалением выбранный файл изменён, подменён ссылкой либо добавлен новый файл; после successful capture/unlink сервис перезапускается до DB commit | Изменённый или заменённый файл требует нового просмотра, включая same-inode запись с прежними size/mtime; новые файлы и данные вне выбранного workspace не удаляются. Immutable intent/private capture и deletion proof сохраняются до эффекта; recovery не делает blind unlink и не считает missing path доказательством удаления. Подмена сохраняется/восстанавливается без перезаписи, неоднозначность требует reconciliation. Request retry возвращает тот же receipt; подтверждённо удалённые файлы не восстанавливаются из старого снимка |
| ENT-AC-60 | Владелец задаёт фильтр N дней при наличии более старых, более новых файлов и файла ровно на границе, затем выбирает часть результата | Показаны только файлы, не изменявшиеся более N суток по серверному времени; изначально ничего не выбрано, удаление требует ручного выбора и подтверждения; невыбранные файлы сохраняются |
| ENT-AC-61 | В чате есть активная задача, включая ожидание, либо запуск задачи конкурирует с очисткой workspace | Просмотр и фильтрация доступны; UI и сервер блокируют удаление при активной задаче без создания/очереди intent; после завершения нужен новый ручной запрос. Cleanup и root admission используют canonical chat lock. Незавершённый принятый cleanup блокирует новую Task до admission, включая после сбоя; existing duplicate receipt неизменен. Очистка не пересекается с исполнением задачи; GET receipt не возобновляет мутацию |
| ENT-AC-62 | Через UI или A2A поступает файл с занятым именем, в том числе несколько таких вложений одновременно; owner перечитывает историю после публикации | Существующие файлы сохранены; новые получают разные свободные имена с суффиксами без перезаписи при гонке; UI, результат приёма и контекст агента содержат фактические имена. История сохраняет scoped actual name/path/size/digest initial и follow-up файлов; pending/excluded имена остаются только в owner review |
| ENT-AC-63 | В сообщении UI или A2A одно из нескольких вложений не загрузилось, не сохранилось либо превышен общий лимит, в том числе после загрузки первых файлов | Всё сообщение отклонено с причиной; текст и частичные вложения не передаются агенту и не публикуются в workspace, включая после restart; существующие файлы и активная задача сохранены |
| ENT-AC-64 | Агент готовит исходящее сообщение UI или A2A с файлами, каждый из которых меньше лимита, но сумма больше него | Отправка отклоняется до частичной публикации; агент получает ошибку с размерами и может уменьшить набор, исходные файлы сохранены; используется общий с входящими вложениями лимит, default 25 МБ. Выбор core_response_files заменяет весь набор, [] очищает, ошибочный новый выбор сохраняет прежний; receipt/manifest атомарны. Live/GetTask/SSE/recovery выдают те же frozen ordered snapshots после изменения originals, включая budget-partial result; corrupt batch не выдаётся частично. Owner download повторно проверяет Task/chat scope, одинаковые bytes/известный ID другого чата не открывают файл |
| ENT-AC-65 | История чата стареет, контекст суммаризируется, владелец очищает выбранные файлы workspace | Полная история сообщений остаётся доступной с прежними правами; автоматического удаления по возрасту нет, очистка затрагивает только выбранные файлы. Owner history API страницы включают все canonical roots, local transcript и каждый final result один раз; provider replay/hidden reasoning исключены. Принятое unread уточнение видно сразу, delivery/rejection не меняет ID и не дублирует entry. Guard material выводится placeholder с scoped review, без raw fallback. Cursor сохраняет позицию после новых roots, append, доставки и compaction; invalid/foreign cursor, child или unmapped legacy run не открывают данные. Reads не вызывают модель/tools/classifier, не меняют waits/budget; external/dual-role403, другая company404. Legacy snapshots без новой provenance читаются детерминированно без потери transcript |
| ENT-AC-66 | Caller повторяет создание с тем же messageId, в том числе одновременно, после restart или замены токена; отдельно повторяет его с изменённым содержимым | При том же содержимом возвращается одна и та же Task без повторного выполнения и копий файлов; изменённое содержимое даёт конфликт; текущие права и scope проверяются |
| ENT-AC-67 | Авторизованный HTTP-ответ остаётся открытым дольше срока токена; затем приходит новый запрос по тому же TCP-соединению или новая подписка | Внутри открытого ответа повторной авторизации и закрытия по истечению токена нет; новый HTTP-запрос проходит новую проверку и с недействительным токеном отклоняется; Task не отменяется |
| ENT-AC-68 | Загрузка отклонена или после сбоя остались временные файлы; рядом есть активные загрузки и принятые файлы, ожидающие guardrails | При обычном отказе временные файлы удаляются сразу, осиротевшие остатки — в течение 24 часов с восстановлением очистки после downtime; активные и принятые данные сохраняются |
| ENT-AC-69 | Агент ждёт до 18:00, в 15:00 принято уточнение в ту же задачу | Ожидание завершается с причиной прихода сообщения, агент сразу возобновляется и получает уточнение перед моделью; прежний таймер не запускает второе продолжение и не считается подтверждением внешнего события |
| ENT-AC-70 | После пробуждения по сообщению агент снова вызывает ожидание, затем приходит дубликат сообщения, старое событие таймера или происходит restart | Новое ожидание сохраняется до собственного повода пробуждения; повтор уже принятого messageId и старый таймер не будят его, продолжение выполняется однократно |

Проверки конкурентности должны действительно создавать гонку. Перезапуски
проверяются с PostgreSQL и сохранёнными файлами. Для суммаризации нужны случаи
с поздними исправлениями, длинными логами и повторной compaction; проверка
наличия заголовков summary недостаточна. Для guardrails проверяются механика
интеграции и обработка заданных вердиктов; оценка качества детектора на наборе
данных исключена из объёма по [контракту детектора](security-and-reliability.md).


### Дополнительные owner-interaction criteria

- [ ] CAS policy/settings не теряет конкурирующие изменения; новый origin под
  прежним alias требует HITL. Owner allow не закрывает прежний pending request,
  deny закрывает pending во всех чатах до нового dispatch.
- [ ] После HITL/restart изменённая MCP schema или исчезнувший tool не исполняются
  по прежнему подтверждению. Новые identities не расширяют принятые права;
  недоступный optional tool скрыт, исходные catalog и digest checkpoint сохранены.
- [ ] У Python prefix есть один проверяемый side effect, nested call ожидает
  решение, remainder имеет другой side effect. После restart/allow nested call
  выполнен один раз, prefix не повторён, remainder не выполнен; модель получает
  interrupted result. При reject/timeout nested call не исполняется.
- [ ] Неподтверждённый Python teardown и неизвестная nested mutation не становятся
  safe wait и не replay-ятся. Решение не позволяет обойти новый deny или ceiling.
- [ ] Python перехватывает исключение вложенного инструмента: неизвестный исход
  мутации остаётся `SIDE_EFFECT_UNKNOWN` и после подтверждённой очистки `ABORTED`;
  без неё сохраняется nonterminal барьер. Исчерпание бюджета остаётся partial
  с финализацией без tools. Отклонённое начисление фонового target не увеличивает usage.
- [ ] Root завершает ответ при работающем optional local child: terminal commit
  и допуск следующей Task в чат происходят только после остановки всего subtree.
  Cleanup failure оставляет чат занятым; чужой server receipt не считается очищенным.
- [ ] Потерявший lease worker не останавливает новую generation. Resume terminal
  intent после restart не повторяет команду; follow-up во время очистки запускает
  следующую команду в новой generation с сохранёнными файлами. Конкурентная очистка
  публикует ровно один непустой snapshot с прежними файлами.
- [ ] Вложенный joined delegate из Python возвращает результат child через
  исходный outer tool call. Изменение `allow` на `require_hitl` во время остановки
  создаёт approval с конечным настроенным timeout, по умолчанию 24 часа.
- [ ] Sentinel внутреннего вопроса/ответа отсутствует во всех external
  Get/List/Subscribe/stream/push/Artifact/metadata до и после restart; owner API
  сохраняет исходную внутреннюю переписку.

Remote scheduler proof LONG-01/02: memory и PostgreSQL проходят одинаковые проверки
claim/due/expiry/CAS. Checkpoint и terminal notification/outbox атомарны; после
crash с Send intent без remote ID повтор Send запрещён, с известным ID — только
GetTask. Race timeout versus result сохраняет ровно один исход/notification,
late claim не публикует материал; повтор wait/get/cancel после timeout не делает
network request. Shutdown не становится caller cancel; tenant/owner чужой
операции и mailbox имеют not-found/пустую scoped выборку. Старые local rows с
null checkpoint и их terminal semantics не меняются.

Remote runtime proof: pin destination revision/settings/message ID до HITL,
одна atomic admission локального handle и parent continuation; approved pending
call сохраняет binding при owner rotation/disable. Повторный call ID в новом
attempt создаёт новое намерение, recovery прежнего attempt возвращает прежний
handle. Top-level и вложенный Python используют один fenced dispatch flow.
Remote wait отклоняет любое явно переданное `timeout`, не продлевая deadline;
пустой effective registry скрывает Send в model catalog/Agent Card и блокирует
новый dispatch. Scoped peer credentials/root identifiers не наследуются извне.

Remote file selection proof: workspace содержит выбранные и невыбранные файлы,
а final response имеет свой выбор. Только явный `core_agent_send_message.files`
передаётся в обеих bindings; отсутствие/[] передаёт один text Part. Invalid,
symlink/foreign path, отсутствующий или последний oversized файл дают zero Send
и zero handle, не меняют originals/final selection. После HITL/restart/изменения
originals отправляются frozen ordered bytes и empty file. Safe approval receipts
соответствуют selection digest; private refs не проходят в model/UI/public A2A.
v1 text-only jobs восстанавливаются прежним способом; v2 source root/child scope
не позволяет загрузить чужой manifest даже с одинаковыми bytes/известным ID.
Malformed/noncanonical/oversized remote batch не relay-ится частично. Human-wait
не импортирует ранние files. Completed terminal result и quarantine acceptance
атомарны при реальном PostgreSQL pool1; cancel/deadline/lease races не оставляют
accepted files. Root/child guard allow/reject/timeout/restart соблюдают общий
publication barrier без shadow Task или обхода canonical ancestry.

Remote progress proof: working/input-required/auth-required metadata появляются
в текущем root Task и scoped Get/List/Subscribe/push без raw peer data и чужих
операций. Pending wait, workflow version и model/tool usage не меняются;
одинаковый polling не повторяет status событие, stale SDK save не стирает progress.
Checkpoint/progress атомарны; malformed progress отвергается без изменения
revision/checkpoint. Timeout и terminal root подавляют позднее progress, PG
projection/push rollback сохраняет прежние гарантии terminal reconciliation.

Owner UI static proof: без bearer доступны только configured shell и
перечисленные compiled assets; credentials/private API остаются закрыты.
Unknown API/A2A/path/asset, traversal, symlink и внепакетный файл не получают
HTML fallback. CSP ограничивает connections deployment issuer origin,
scripts/styles self, shell не кешируется. Disabled client/absent build дают404.
Synthetic static fixture доказывает routing/security, но не SPA build,
реальный Keycloak login и browser interactions.
