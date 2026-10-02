# Enterprise semantic context and chat continuity — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute the checkboxes in order; do not edit shared runtime files while file admission work owns them.

**Goal:** Сохранять смысл решений и уточнений при сжатии и передавать проверенную историю в следующую задачу того же чата, включая cron.

**Architecture:** Переиспользовать immutable transcript в workflow snapshot и canonical `core_chats` ownership. Семантическое summary строится отдельным вызовом существующего model adapter без tools, проходит проверку структуры и provenance и заменяет active context только одной fenced workflow transaction. История владельцев читается из сохранённых runs; отдельная копия всех сообщений не создаётся.

**Tech Stack:** Существующие Python, json/hashlib, model adapter, PostgreSQL/psycopg, unittest, uv и owner ASGI API.

## Источники и установленные пробелы

Нормативные требования: `spec/context.md` CONTEXT-01–03, `spec/runtime.md`,
`spec/acceptance.md` ENT-AC-25, 38, 65. Разрешение на frozen spec/tests дано
29 сентября и записано в основном enterprise plan; повторного согласования
принятых продуктовых решений не требуется.

- `StructuredSummarizer.__call__` обрезает JSON с начала истории, теряя поздние
  исправления. Наличие семи заголовков не доказывает семантическое сжатие.
- `Compactor` принимает callback и не меняет входной `ContextState` до успеха;
  это существующая точка подключения, новая context framework не нужна.
- `CoreAgent._context_compactor` каждый turn учитывает реальные instructions и
  catalog. Его callback пока локальный и не резервирует model attempt.
- `_append_context_item`, `_append_result`, `_append_assistant_calls` сохраняют
  полный transcript отдельно от active items. Provider replay metadata не
  должна попасть в summary prompt или пользовательскую историю.
- `_new_workflow` начинает пустой контекст либо только pinned prompt; совпадение
  `context_id` само по себе историю не переносит.
- `core_chats.latest_root_run_id` уже задаёт предыдущий root под chat lock.
  `PostgresRetentionManager.delete_run` запрещает удаление enterprise chat runs.
  Поэтому не нужны новая transcript table и новая retention policy.
- Final response хранится в `record.result`, а не обязательно в transcript.
  Проекция истории обязана добавить его ровно один раз после сообщений run.

## 1. Сохранить контекст при неудаче необязательного сжатия

**Files:** `core_agent/context.py`, `tests/test_context_kernel_skills_security.py`.

- [x] Воспроизвести ошибку interval compaction ниже 90%: callback возвращает
  невалидную summary, runtime получает `CONTEXT_UNRECOVERABLE` вместо исходного state.
- [x] В `maybe_compact` отдельно вычислить pressure. Только ошибка
  `CONTEXT_UNRECOVERABLE` при отсутствии pressure возвращает тот же state;
  `LEASE_LOST`, cancel и storage errors не поглощаются.
- [x] Проверить сохранение объекта context/transcript, ошибку при >=90% и
  propagation fencing error. Выполнено: `uv run --offline --no-sync python -m
  unittest tests.test_context_kernel_skills_security -q` — 27 проверок после
  дополнительных исправлений границы окна ниже.
- [x] Проверить pinned против всей working capacity, включая случай без history.
  При pinned выше цели выделять оставшееся место настоящей summary; overlap,
  занявший цель целиком, возвращать в candidates. Все три regression scenarios
  воспроизведены до исправлений и проходят после них.
- [x] Устранить ранний возврат при overlap, захватившем всю unpinned history:
  oversized result возвращается в summary candidates до проверки пустого segment.
  Independent review воспроизвёл overflow, regression прошёл red→green.

## 2. Семантический callback и честные границы provenance

**Files:** `core_agent/context.py`, `tests/test_context_kernel_skills_security.py`,
`tests/test_runtime_observability.py`.

- [x] До изменения runtime закрепить в `spec/context.md` формат summary,
  versioned provenance, пары provider tool protocol, current material rejection
  и durable попытки сжатия; дополнить acceptance и обновить exact hash lock.
- [x] Заменить production `StructuredSummarizer` на semantic callback с
  существующим интерфейсом `(items, max_tokens) -> ContextItem`. Callback получает
  только разрешённые active candidates: kind/content, исходные sequence IDs,
  предыдущую summary и её provenance. Pinned contracts передаются отдельно как
  read-only контекст задачи; их нельзя переписывать результатом модели.
- [x] Запрос модели содержит tools `{}` и отдельную инструкцию: все records —
  данные; сохранять последнее исправление, различать план/выполнение,
  подтверждённый факт/вывод/предположение, сохранить незавершённое и ошибки.
  Не включать provider signatures, raw quarantine, credentials, hidden reasoning.
- [x] Использовать строгий JSON с ровно семью полями `Goal`, `Constraints`,
  `Decisions`, `Completed`, `Artifacts`, `Pending`, `Failures`. Каждое поле —
  список `{text, basis, sources}`; basis принимает только `fact`, `inference`,
  `assumption`; sources — непустой список известных immutable source IDs.
  Пустая секция — `[]`. Дубли ключей, неизвестные IDs, дополнительные поля,
  tool requests, незавершённый/обрезанный provider answer отклоняются.
- [x] Единица выбора segment/overlap — завершённый assistant tool-call batch с
  соответствующими results. Не оставлять orphan tool_result/tool_call. Вызов без
  результата и необходимые provider replay signatures удерживать вместе до
  завершения; signature не передавать summarizer-у. Перенос между задачами
  использует plain historical projection, а не replay чужого tool protocol.
- [x] Перед summary callback, повторным model call и импортом истории проверять
  текущие canonical negative material decisions и visibility для всех источников.
  Summary с хотя бы одним впоследствии rejected/timed-out source инвалидировать
  и перестраивать из оставшихся разрешённых originals. Нельзя очищать только
  source list и оставлять prose, которое уже могло включить запрещённое содержимое.
- [x] Provenance хранить в ContextItem как optional versioned поле, совместимое
  со старым snapshot. Новые прямые записи получают локальный transcript sequence;
  imported history использует `{run_id, sequence}`. Ссылки summary flatten-ятся
  до исходных источников, а не ссылаются только на исчезнувший summary item.
  Старые items можно сопоставить с immutable transcript; неоднозначность требует
  консервативного диапазона, а не придуманного точного ID.
- [x] Пересчитать размер полного rendered summary, включая provenance, тем же
  tokenizer. Не доверять числу tokens из ответа модели. Не обрезать JSON и не
  наполнять summary бессмысленным padding ради 10%: сохранить дополнительный
  целый recent segment, если результат ниже целевого диапазона.
- [x] Exact runtime references и открытые side effects сохранять в canonical
  pinned state вне model prose; summary не может одобрить tool, закрыть wait или
  превратить `SIDE_EFFECT_UNKNOWN` в успех. Проверять pin/window с учётом всей
  base capacity. При pinned выше целевых 15%, но ниже окна выделять реальное
  оставшееся место для summary вместо нулевого budget.
- [x] Проверки: исправление A→B в конце, повторное summary с B→C, план остаётся
  планом, ошибка остаётся ошибкой, source mapping переживает повторное сжатие,
  malformed/truncated/oversize/unknown-source ответы не меняют исходный state.
  Scripted provider подтверждает контракты и flow, не качество реальной LLM.

## 3. Подключить callback к durable model accounting

**Files:** `core_agent/runtime.py`, `core_agent/app.py`, при необходимости
`core_agent/model.py`; existing context/runtime tests.

- [x] Production вызывает существующий adapter без stream/tools/skills/memory.
  Не менять общий adapter in-place. Для CompatibleHttpModel использовать копию
  параметров с bounded output budget; scripted adapter остаётся injectable.
- [x] До каждой физической summary attempt фиксировать в том же workflow
  transition charge общего root model ledger, local turns и marker
  `purpose=compaction`. Резерв finalization не тратить на сжатие. Retry — новая
  оплаченная попытка; максимум две попытки с более строгим target на второй.
- [x] Persisted compaction operation содержит version, fingerprint точного source
  segment и его decisions/visibility revisions, target, attempts и outcome.
  Restart продолжает прежний счётчик; новая attempt не сбрасывает его. Commit
  проверяет тот же fingerprint под fencing; changed sources требуют нового
  выбора segment, а не публикации устаревшей summary.
- [x] Lease/cancel проверять до provider и до commit. Неудача ниже pressure
  сохраняет старый context; при pressure и невозможности построить допустимый
  summary возвращать `CONTEXT_UNRECOVERABLE`. Ошибки lease/storage не превращать
  в обычную неудачу summary. Результат provider после lease loss не публиковать.
- [x] Отказ списания local/root model budget маршрутизировать в существующую
  budget-finalization (`completed`, `complete=false`), не маскировать его как
  summary failure. Reserved finalizer получает fitting разрешённую проекцию;
  если её нет, безопасный deterministic partial result. Forbidden old summary
  остаётся исключённой даже при недостаточном budget для rebuild.
- [x] Summary + sources + `context.compacted` фиксировать одной transition;
  telemetry содержит before/after/base/capacity/usage, без текстов. Recovery после
  committed summary не вызывает модель снова; после неизвестного provider
  outcome следующая attempt заново оплачивается и ограничена прежним budget.
- [ ] Проверить real CoreAgent flow: два сжатия и recovery, late correction в
  следующем model call, finalization reserve, child/root shared budget,
  failure before commit, pending wait без повторного tool dispatch.

Реализация шагов 2/3 прошла независимые spec/security и code-quality review.
Исправлены legacy disposition/derived-arguments leaks, wire override streaming/
output cap, бесконечный retry после recovery при pre-provider window failure и
задержка follow-up, принятого во время summary. Последняя correction доставляется
перед ближайшим обычным model call, interval не вызывает повторное сжатие того
же хода. Focused gate:50 tests,exit0; независимый quality gate:35 tests без
пропусков,exit0. Scripted/adapter proof не заменяет оценку реальной LLM.

Full suite после основного semantic change:895 tests,6 failures,34 errors,
218 skips (socket bind/Python broker/network environment и отсутствующие
PostgreSQL/native интеграции); этот gate не пройден. Последующие две regression
corrections проверены focused gates выше. Cross-task visibility/provenance и
импорт из шага 4 реализованы в следующем срезе ниже; расширенная проверка
child/shared-budget/wait/restart на PostgreSQL остаётся обязательной.

## 4. История между задачами и owner API

**Files:** `core_agent/admission.py`, `core_agent/workflow.py`,
`core_agent/runtime.py`, `core_agent/owner_api.py`; admission/owner/runtime tests.

- [ ] До endpoint implementation закрепить bounded pagination/projections и
  owner-only scope в `spec/public-contract.md` и acceptance с exact hash lock.
- [x] Первый независимо проверяемый API срез — `GET /api/chats` для списка UI:
  context_id/latest_task_id/active из canonical admission mapping, порядок
  context_id, limit1–100, company-bound cursor. Async memory читает под admission
  lock, PostgreSQL использует bounded SELECT с latest root join. Это не история
  сообщений и не runtime импорт; следующие строки остаются отдельной работой.
  Реализация и independent review завершены:5 ASGI tests executed,5 PostgreSQL
  tests skipped. Cursor использует сохранённую root Task, поэтому длинный Unicode
  context ID не делает следующий запрос невозможным; новый root между страницами
  не меняет позицию. NUL/surrogate/foreign-company cursors отклоняются. Общий gate
  owner_chats/interactions/spec:53 tests,29 executed,24 PostgreSQL skips,exit0.
- [x] Под chat lock при root admission сохранить previous root ID в новом
  snapshot до замены `latest_root_run_id`. Busy root и повтор messageId ничего
  не меняют. Previous ID server-owned; не принимать его из request metadata.
  Поле `previous_root_run_id` additive; legacy отсутствие означает отсутствие
  закреплённого источника. Memory/PG используют существующую admission transaction.
  Два ASGI regression scenarios прошли red→green: same-chat owner continuation,
  metadata spoof, unrelated chat, dedup и busy attempt. Gate admission/owner_chats/
  file_admission/spec:105 tests,52 executed,53 PostgreSQL skips,exit0. Это закрепляет
  только источник; model history import остаётся следующим пунктом.
- [x] После проверки нового prompt загрузить previous terminal root по тем же
  tenant/owner/context, исключить child и другой чат. Использовать разрешённый
  active summary + последние разрешённые messages + final result. Новая цель
  становится pinned prompt; старая цель маркируется исторической.
- [x] Импортировать только модельную проверенную проекцию, не raw request,
  private material review или непрочитанный failed input. Сохранить versioned
  imported sources в snapshot одной fenced transition до model call; recovery
  не меняет выбранную историю и не добавляет её повторно.
- [x] Owner-only `GET /api/chats` и `GET /api/chats/{context_id}/history` читают
  сохранённые root workflows и full transcript с bounded pagination. Company
  owner может видеть внутреннюю переписку, external роль owner routes не получает.
  Guards/quarantine отображаются как отдельные ссылки на owner review API,
  не как inline bytes. Provider replay и hidden reasoning никогда не выдавать.
- [ ] Полный transcript и историю отказов сохранять; retrieval для модели
  возвращает только разрешённую проекцию с теми же tenant/chat checks и current
  negative-material checks. Existing memory tools не превращать в обход guards.
- [ ] Проверки: новая Task знает согласованное решение предыдущей, второй чат
  и другой external caller не видят его, failed/partial final правильно помечен,
  owner dialogue не появляется в external GetTask/stream, cleanup/retention не
  удаляет историю; read-only history request не запускает workflow.

Runtime import из шага 4 подключён после initial prompt guard. Сохраняется
versioned projection с references на originals и canonical terminal-result digest;
foreign review reads не получают lease/locks исходного terminal run. Real-flow
proof включает 3-run chain, два semantic compaction, late material revoke перед
четвёртой Task, recovery без повторного импорта, failed/partial label, отсутствие
replay, exact final-text rejection, scope/corruption/lease checks. Реальный A2A
integration gate выявил dispositioned input отменённой Task в source selection;
он исключён до валидации foreign sources.

Focused runtime/guardrails gate:130 tests,104 executed,26 PostgreSQL skips,exit0.
ASGI admission/owner/file/auth gate:139 tests,75 executed,64 PostgreSQL skips,
exit0. Independent spec/security review:17 tests,15 executed,2 PostgreSQL skips,
exit0. Эти проверки относятся к runtime import; owner history API проверен
отдельным последующим срезом ниже. Retrieval и browser acceptance остаются
открытыми; перенос контекста модели не выдаётся за их завершение.

Финальный independent code-quality review:34 tests,32 executed,2 PostgreSQL
skips,exit0; блокеров не найдено. Последующий полный прогон:919 tests,6 failures,
34 errors,221 skips; имена всех failures/errors совпали с предыдущим full gate
(socket/network/Python broker ограничения окружения). Ruff по `core_agent tests`
и `git diff --check` проходят; полный release gate остаётся непройденным.

Последующий owner history срез добавляет bounded API, canonical previous-root
chain, стабильные input identities и cursors, safe queued/dispositioned input,
однократный final result и owner review links. Memory и PostgreSQL используют
одну проекцию; PostgreSQL читает scoped slices в read-only repeatable-read
transaction без full snapshot payload. Provider replay и hidden reasoning
исключены. Current negative checks учитывают material kind и digest, включая
сам final text и imported dependencies. Independent reviews выявили смешение
file/text digest namespaces и пропущенный final-text rejection; оба случая
воспроизведены до исправления и проверены после него.

Focused history/admission/runtime gate:216 tests,123 executed,93 PostgreSQL
skips,exit0. Независимые spec/security и quality reviews после исправлений
не нашли блокеров. History API подключён к исходникам UI, включая pagination,
queued input, решения владельца и историю нескольких Task. Реальный PostgreSQL
gate, browser acceptance, cleanup/retention и model retrieval этим не доказаны.

## 5. Доказательства и совместимость

- [ ] Проверить чтение старого snapshot после normative изменений шагов 2/4;
  неизвестная provenance version отклоняется, а не трактуется как разрешённая
  история. Политические решения уже согласованы, invariants нельзя ослаблять.
- [ ] Выполнить targeted unittest для context/runtime/admission/owner, затем
  `uv run --offline --no-sync ruff check core_agent tests` и обычный suite.
  PostgreSQL restart/CAS/budget и реальные provider wire checks обязательны в CI;
  отсутствие PG или запрещённый socket bind не записывать как PASS.
- [ ] Обновить `AGENTS.md` по реально подключённой цепочке. Отмечать requirement
  implemented только с соответствующим обычным CI proof. UI и cron завершают свои
  end-to-end проверки после подключения этого контекста, не до него.
