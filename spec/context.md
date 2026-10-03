# Контекст и суммаризация

## Цель

Ядро должно продолжать длинную задачу и session без переполнения окна и без молчаливой потери активной цели. Суммаризация, retrieval и context assembly являются частью runtime, а не обязанностью клиента или skill.

## Слои контекста

Context Engine собирает каждый model call заново из:

1. platform/host instructions;
2. текущего prompt и working state;
3. непосредственной transcript history;
4. активных skills и нужных tool schemas;
5. результаты разрешённых retrieval MCP и памяти агента, если она включена;
6. релевантных artifact excerpts;
7. summaries старых segments и child Tasks.

Каждый элемент имеет source, priority, token cost, freshness и provenance. Наличие данных в transcript не гарантирует включение в следующий model call.

## Разделение окна

Для выбранной модели ядро получает полное окно `C` и отдельно считает fixed/base и working части:

```text
base_tokens = system_and_kernel_tokens + required_tool_schema_tokens + output_reserve
working_capacity = C - base_tokens
working_occupancy = active_working_tokens / working_capacity
```

Где:

- `system_and_kernel_tokens` — platform/host policy, KernelInstructions и AgentProfilePrompt;
- `required_tool_schema_tokens` — schemas, которые будут переданы модели;
- `output_reserve` — гарантированное место для следующего ответа модели;
- `active_working_tokens` — prompt, transcript, summaries, retrieved memory, task notifications и artifact excerpts.

Ядро MUST использовать tokenizer выбранной модели либо консервативную оценку с запасом, если tokenizer недоступен.

System prompt и tool schemas MUST NOT учитываться в 90%/10–15% working threshold. Они всё равно физически занимают `C`, поэтому сначала вычитаются вместе с output reserve. Если `base_tokens` не оставляет минимальную working capacity, model route отклоняется.

`base_tokens` пересчитывается перед каждым model call из фактически активных
инструкций и публикуемых schemas. Подключение полного `SKILL.md` или появление
`core_skill_read_resource` не может использовать расчёт предыдущего turn.

## Порог 90%

Compaction MUST запускаться до model call, если прогнозируемый `working_occupancy >= 0.90`.

Дополнительно ядро SHOULD оценивать размер нового tool result до добавления в активный контекст. Большой result сначала переводится в artifact и краткое представление, чтобы не пересечь hard limit внезапно.

После compaction активный working context MUST занимать от 10% до 15% `working_capacity`. Runtime целится в 15% и MAY снижать до 10%, если иначе не сохраняется безопасный запас или ожидается большой tool result.

Ниже 10% сжимать SHOULD NOT: чрезмерная компрессия повышает риск потери полезной локальной истории. Выше 15% compaction считается неуспешным и повторяется с более строгим budget.

## Что нельзя суммаризировать

Следующие данные являются pinned и сохраняются дословно либо в канонической структурированной форме:

- safety-инварианты и host policy;
- исходный пользовательский prompt;
- определения активных tools, нужные следующему шагу;
- инструкции активных skills, пока skill нужен задаче;
- незавершённые tool calls и их идентификаторы;
- текущие hard limits;
- точные пути изменённых файлов и ссылки на созданные artifacts;
- последние сообщения, без которых следующий шаг теряет непосредственный смысл;
- provenance и revision извлечённых memory records, влияющих на текущее решение;
- parent/child Task contracts и ещё не проверенные child results;
- принятые, но ещё не доставленные model loop inbound Messages с их sequence/provenance.

Если pinned data вместе со всей fixed/base частью не помещается в окно
(`pinned_tokens > working_capacity`), запуск MUST завершиться с
`CONTEXT_UNRECOVERABLE`, в том числе когда summarizable history отсутствует.
Ядро MUST NOT молча обрезать pinned data.

Сравнение идёт именно с окном, а не с целью сжатия: цель — доля working capacity, она в разы меньше окна, и pinned data, свободно помещающаяся в контекст, не является основанием завершить запуск. Overlap в этом сравнении не участвует, потому что состоит из unpinned элементов: их можно отпустить, и MUST отпускать, начиная с самых старых, пока удерживаемое не уложится в цель. Отпущенный элемент возвращается кандидатом в summary, а не теряется.

Цель сжатия является ориентиром, а не условием выживания запуска. Если один pinned больше цели, но помещается в окно, compaction MUST выполниться с наилучшим достижимым результатом. Обратное правило означает, что накопленная pinned data убивает исправный запуск ровно в тот момент, когда её решили ужать, — и тем вероятнее, чем полезнее была работа.

При pinned не меньше цели runtime выделяет summary до одной целевой доли
working capacity из фактически оставшегося места; итог всё равно не превышает
working capacity. Нулевой summary budget не является корректной реализацией
этого исключения, если место осталось. Overlap, занявший всю цель, отпускается
до выделения места summary; сам по себе overlap не разрешает превысить 15%.

Принудительное сжатие по `EVENTS_COMPACTION_INTERVAL` подчиняется тем же правилам. Оно является оптимизацией расписания, а не признаком нехватки места, поэтому его неудача MUST NOT завершать запуск, который по заполненности контекста в сжатии не нуждался.

## Что суммаризируется

Кандидаты по порядку:

1. завершённые tool outputs;
2. старые промежуточные объяснения модели;
3. завершённые ветви исследования;
4. предыдущая summary вместе с новым отрезком истории;
5. неактуальные retrieved memories и tool schemas, не нужные следующему шагу.

Полные outputs остаются в artifact store и audit transcript. В активном контексте остаются ссылка, тип, размер, digest и краткое релевантное содержание.

## Формат summary

Summary MUST быть структурированной и содержать:

```text
Goal: исходная цель без переинтерпретации
Constraints: действующие ограничения
Decisions: принятые решения и основания
Completed: завершённые действия и проверенные результаты
Artifacts: файлы, IDs, digests и места полных outputs
Pending: незаконченные шаги и открытые вопросы
Failures: существенные ошибки и уже проверенные неудачные подходы
```

Summary MUST различать подтверждённые факты, выводы модели и предположения. Для длинных sessions допускается иерархия turn -> run -> session summaries; каждый уровень хранит provenance на нижележащие immutable events.

В persisted summary version 1 эти семь секций представлены JSON object с ровно
ключами `Goal`, `Constraints`, `Decisions`, `Completed`, `Artifacts`, `Pending`,
`Failures`. Значение каждой секции — список объектов `{text, basis, sources}`:
непустой текст, `basis` из `fact|inference|assumption` и непустой список source IDs
из переданного runtime набора. Пустая секция представлена `[]`. Source ID имеет
сохранённое отображение на immutable `(run_id, transcript sequence)`, явно
обозначенный исходный диапазон либо terminal-result identity ниже; модель не определяет scope и не создаёт новые
идентификаторы. Повторное summary сохраняет ссылки на originals, а не только
на заменяемое summary. Metadata provider replay не является источником summary.

Новый reader сохраняет чтение прежних ContextItem/snapshot без provenance:
источники восстанавливаются из сохранённого immutable transcript данного run,
а прежний обрезанный summary не получает выдуманные точные citations и не
считается semantic summary. При необходимости он перестраивается из допустимых
originals с теми же budget/visibility правилами. Неизвестная явно указанная
version даёт `CHECKPOINT_INVALID`, не трактуется как legacy. Новые summary и
compaction-operation records имеют собственную version 1 в snapshot; это не
требует добавления второй transcript table. Rollback image допустим только
на версию, умеющую читать уже записанные snapshot formats.

Неизвестные source IDs, повторные JSON keys, дополнительные поля, неверные типы,
tool calls и незавершённый/truncated ответ провайдера делают summary невалидной.
Размер пересчитывается runtime по полному model-facing представлению: число
tokens из текста модели не является authority. JSON или serialized transcript
нельзя обрезать до целевого размера. Если summary меньше целевого диапазона,
допускается сохранение дополнительных целых recent segments; padding не является
полезной историей. Exact artifact references и открытые side effects остаются
каноническими pinned данными независимо от текста summary.

Assistant tool-call batch и все его results образуют неделимую единицу выбора
segment/overlap. Активный provider context MUST NOT содержать orphan tool_result
или tool_call без результата вследствие compaction. Незавершённый вызов и
необходимая для его продолжения replay metadata удерживаются вместе. Между
задачами импортируется historical data projection, а не незавершённый provider
tool protocol предыдущей задачи.

## Процедура compaction

1. Зафиксировать границу исходного транскрипта.
2. Выбрать самый старый непрерывный summarizable segment.
3. Построить семантическую summary с учётом предыдущей, поздних исправлений и подтверждённых outcomes; обрезка сериализованной истории не является суммаризацией.
4. Проверить наличие pinned facts и структурных секций.
5. Пересчитать base/working tokens отдельно.
6. Атомарно заменить segment на summary в активном контексте.
7. Сохранить mapping summary -> исходные event sequence ranges.
8. Испустить `context.compacted` без содержимого приватных данных.

Неудачная summary не должна частично изменять активный контекст. Ядро MAY повторить compaction один раз с более агрессивным размером, затем MUST вернуть `CONTEXT_UNRECOVERABLE`.

Семантическая модель вызывается без tools, retrieval и mutation authority.
Каждая физическая попытка, включая повтор после неизвестного outcome, заранее
списывается из общего model budget и local usage; finalization reserve не
используется для summary. До первого вызова сохраняется versioned compaction
operation: fingerprint выбранных sources/границы и decisions/visibility revisions,
target и число attempts. Restart не обнуляет максимум двух attempts одной
операции. Commit проверяет прежнюю границу, актуальную видимость и workflow
lease и атомарно сохраняет summary, provenance и `context.compacted`. Уже
committed summary не требует повторного вызова модели при recovery.

Ошибка summary при необязательном interval compaction ниже pressure threshold
сохраняет прежний context. Ошибки lease/cancel/storage не поглощаются этим
правилом. Если context требует сжатия и после допустимых попыток безопасного
результата нет, возвращается `CONTEXT_UNRECOVERABLE` без частичной замены.

Отказ списания model budget не является неудачной summary. При исчерпанном
local/root execution budget применяется существующая budget-finalization:
Task завершается `completed`, `complete=false`,
`completion_reason=budget_exhausted`. Finalization reserve не расходуется на
сжатие. Если прежнее summary запрещено или не помещается, finalizer получает
помещающуюся разрешённую проекцию либо используется безопасный детерминированный
partial result; запрещённое prose не возвращается в context ради завершения.

## Tool catalog disclosure

Большой MCP catalog не должен целиком попадать в каждый model call. Context Engine MUST поддерживать:

1. metadata index всех tools;
2. компактный discovery/search interface;
3. полную schema только отобранных tools;
4. кеширование выбора в пределах текущей цели;
5. invalidation при изменении MCP catalog revision.

Critical built-in tools MAY быть всегда видимы. Tool search result не даёт разрешение на исполнение.

## Retrieval и забывание

Retrieval MCP results имеют отдельный budget и учитываются до финального working occupancy check. Result MAY быть вытеснен из active context без изменения внешнего service. Core cache MUST учитывать server/index revision; delete/tombstone notification инвалидирует соответствующие excerpts.

## Проверка качества summary

Перед commit Context Engine выполняет structural checks и, для high-risk runs, MAY использовать независимый verifier model. Verifier проверяет только сохранность goal, constraints, facts, open side effects и provenance; он не заменяет summary новым планом.

## Проверяемые гарантии

- Исходный prompt побайтно совпадает до и после любого числа compaction.
- Ссылки на изменённые файлы и artifacts не теряются.
- После compaction runtime может объяснить, какие sequence ranges были заменены.
- Полный audit transcript не изменяется при compaction.
- Metrics/event показывают base tokens, working tokens, working capacity и before/after working occupancy раздельно.

## Непрерывность чата

### CONTEXT-01. Контекст между задачами

Новая задача получает релевантное summary чата, последние сообщения и новую
инструкцию. Это относится и к повторным cron-запускам. Один `contextId` без
подключения истории не обеспечивает требуемую непрерывность.

Root admission MUST под canonical chat lock сохранить в начальном workflow
snapshot поле `previous_root_run_id`: прежний `latest_root_run_id` того же
tenant/owner/context либо `null` для первого запуска. Сохранение происходит
в той же транзакции, что создание workflow и замена latest root. Поле задаёт
только сервер; request metadata не является источником истории. Duplicate
messageId и отклонённая busy Task не меняют цепочку. Recovery использует
сохранённую ссылку, а не перечитывает изменяемый latest root чата.

Это additive snapshot field; отсутствие в legacy snapshot означает отсутствие
закреплённого previous root, без угадывания по времени или идентификаторам.
Импорт проверяет terminal root и точное совпадение tenant/owner/context до
чтения его model projection. Само наличие ссылки ещё не означает, что история
прошла guards или доставлена модели; delivery фиксируется отдельно.

После проверки нового prompt runtime MUST однократно импортировать допустимую
проекцию предыдущего terminal root: active semantic summary, последние сообщения
и сохранённый итог. Новый prompt остаётся pinned текущей инструкцией; прежние
цели и исходы явно помечены как история. Импорт не создаёт новых provider tool
calls и не переносит replay/signatures, старые budgets, разрешения, waits или
execution state. Failed/partial outcome не превращается в успешное выполнение.

Активированные skills восстанавливаются отдельно от исторической проекции по
[контракту skills](skills.md): новый root проверяет текущие permissions и
package locks, закрепляет полное тело в instruction layer и включает его в
base tokens. Историческая activation receipt не заменяет эту проверку и не
является provider tool call текущей Task.

`context_import` version 1 и выбранная historical projection сохраняются одной
fenced transition текущей Task до обращения к модели. Повторное восстановление
не импортирует её заново. Full transcripts источников не копируются в transcript
новой Task: provenance ссылается на immutable originals. При повторном импорте
сохраняются исходные источники, включая более ранние задачи цепочки.
Неизвестная version, missing source, nonterminal/child source или несовпадение
tenant/owner/context дают `CHECKPOINT_INVALID`, а не тихую потерю истории.

Итог, находящийся в `record.result` вне transcript, имеет server-owned source ID
`<run_id>:result:<digest>` и reference `{kind: terminal_result, run_id,
result_digest}`. Digest — SHA-256 канонического JSON всего сохранённого result;
это отдельная immutable identity, а не выдуманный transcript sequence. Его
dependencies консервативно включают все доставленные originals, от которых мог
зависеть итог, включая источники импортированной истории. Записи с disposition
`unprocessed_due_to_failure|cancel` не являются доставленными источниками.

Перед использованием импортированной summary или итогового текста runtime
проверяет current negative decisions всех dependencies в scope текущего чата.
При инвалидировании допустимые originals читаются по их настоящему source run,
с повторной проверкой terminal/root/tenant/owner/context и result digest; raw
quarantine остаётся недоступен. Эти read-only операции выполняются под lease
текущей Task и переиспользуют её transaction connection, без захвата writer lease
или execution lock завершённых исходных задач.

### CONTEXT-02. Семантическое summary

Суммаризация должна осмысленно сохранять цели, ограничения, решения, последние
уточнения, подтверждённые результаты, ошибки и открытые вопросы. Старое решение,
заменённое новым, не должно оставаться действующим только потому, что оно раньше
встретилось в истории. Запланированное действие отличается от выполненного.

### CONTEXT-03. Источник истины

- Полный transcript сохраняется, summary ссылается на исходные сообщения.
- У истории чатов нет автоматического срока удаления. Compaction и ручная
  очистка файлов не удаляют сообщения из полного transcript.
- Детали можно получить через retrieval с теми же ограничениями доступа.
- Состояния HITL, действий, ожиданий, deadline и окончательных timeout хранятся
  структурированно вне summary.
- Summary не выдаёт разрешение на инструмент и не открывает закрытое ожидание.
- Если нужна гарантия отсутствия повторного действия, её обеспечивает runtime
  state/идемпотентность, а не обещание модели помнить summary.
- Ошибка суммаризации не должна молча заменять предыдущий результат обрезанным
  текстом и терять актуальные ограничения.

Context assembly, summary и retrieval MUST сохранять tenant/caller/chat scope и visibility материалов. Запрещённый guardrails материал и внутренняя переписка владельцев не публикуются внешнему caller-у через summary или историю. Состояния tool policy, HITL, remote/timer waits, deadline и final timeout являются структурированным источником истины независимо от качества summary.

Проверка текущих canonical rejection/timeout decisions применяется перед summary
callback, каждым model call и импортом previous run, включая его final result.
Если любой source ранее сохранённого summary теперь запрещён, его prose тоже
нельзя использовать: summary перестраивается из разрешённых originals. Простое
удаление запрещённого source ID при сохранении текста не является проверкой.
Полный private audit transcript при этом сохраняется с прежними owner правами;
модель получает только разрешённую проекцию.

Owner history объединяет retained inbound messages с полным local transcript,
а не с active summary/import. При приёме follow-up workflow под существующим
lock закрепляет server-owned `history_after_sequence` в inbox provenance —
последнюю committed transcript sequence. Delivery/disposition атомарно сохраняют
`inbound_sequence` и `message_id` в provenance соответствующего transcript item.
Эти additive поля не меняют provenance version1 и не являются материалом для
summary или разрешением доступа. Caller не задаёт их значения. Entry входа всегда
имеет identity retained inbox row; matched transcript item не дублируется.

Порядок внутри root определяется immutable transcript positions и закреплёнными
input anchors. Начальное сообщение root всегда предшествует всем follow-up,
включая приём во время его guardrail/MCP ожидания: anchor0 в display считается
после initial sequence1 без изменения сохранённого anchor или identity входа.
Synthetic initial placeholder сохраняет ID после публикации настоящего item.
Terminal result имеет отдельную окончательную позицию. Roots
читаются newest-first через canonical `previous_root_run_id`. Chain, cursor и
каждый source проверяются по authenticated company, исходному chat owner и
context; children и busy failed receipts без workflow в chain не входят.
Повторная compaction не меняет full transcript sequence.

Для старых consumed inputs без новой provenance полный transcript остаётся
источником; отдельная повторная inbox entry не добавляется. Старый unread input
имеет стабильную legacy позицию после transcript, перед final result. После его
доставки новая provenance связывает transcript с той же inbox identity/позицией.
Точное время приёма legacy данных не выдумывается. Unknown provenance version,
повреждённая chain или foreign source дают safe error без raw snapshot fallback.
Перестроение выполняется только при доступном execution budget; его отсутствие
не разрешает использовать запрещённую старую summary.
