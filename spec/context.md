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

Если pinned data вместе с `output_reserve` не помещается в окно, запуск MUST завершиться с `CONTEXT_UNRECOVERABLE`. Ядро MUST NOT молча обрезать pinned data.

Сравнение идёт именно с окном, а не с целью сжатия: цель — доля working capacity, она в разы меньше окна, и pinned data, свободно помещающаяся в контекст, не является основанием завершить запуск. Overlap в этом сравнении не участвует, потому что состоит из unpinned элементов: их можно отпустить, и MUST отпускать, начиная с самых старых, пока удерживаемое не уложится в цель. Отпущенный элемент возвращается кандидатом в summary, а не теряется.

Цель сжатия является ориентиром, а не условием выживания запуска. Если один pinned больше цели, но помещается в окно, compaction MUST выполниться с наилучшим достижимым результатом. Обратное правило означает, что накопленная pinned data убивает исправный запуск ровно в тот момент, когда её решили ужать, — и тем вероятнее, чем полезнее была работа.

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
