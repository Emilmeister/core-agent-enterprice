# Контекст и суммаризация

## Цель

Ядро должно продолжать длинную задачу и session без переполнения окна и без молчаливой потери активной цели. Суммаризация, retrieval и context assembly являются частью runtime, а не обязанностью клиента или skill.

## Слои контекста

Context Engine собирает каждый model call заново из:

1. platform/host instructions;
2. текущего prompt и working state;
3. непосредственной transcript history;
4. активных skills и нужных tool schemas;
5. retrieved session/user memory;
6. релевантных artifact excerpts;
7. summaries старых segments и child runs.

Каждый элемент имеет source, priority, token cost, freshness и provenance. Наличие данных в transcript не гарантирует включение в следующий model call.

## Расчёт заполнения

Для выбранной модели ядро получает эффективное окно `C` от provider adapter. Перед каждым model call вычисляется:

```text
occupancy = (active_input_tokens + required_tool_schema_tokens + output_reserve) / C
```

Где:

- `active_input_tokens` — все выбранные сообщения, memory, excerpts и активные инструкции следующего вызова;
- `required_tool_schema_tokens` — schemas, которые будут переданы модели;
- `output_reserve` — гарантированное место для следующего ответа модели.

Ядро MUST использовать tokenizer выбранной модели либо консервативную оценку с запасом, если tokenizer недоступен.

## Порог 80%

Compaction MUST запускаться до model call, если прогнозируемый `occupancy >= 0.80`.

Дополнительно ядро SHOULD оценивать размер нового tool result до добавления в активный контекст. Большой result сначала переводится в artifact и краткое представление, чтобы не пересечь hard limit внезапно.

После compaction целевой `occupancy` MUST быть не выше 0.60. Thresholds являются стабильными defaults продукта; host MAY сделать их строже, но не выше 80% без capability-specific доказательства безопасного output reserve.

## Что нельзя суммаризировать

Следующие данные являются pinned и сохраняются дословно либо в канонической структурированной форме:

- safety-инварианты и host policy;
- исходный пользовательский prompt;
- определения активных tools, нужные следующему шагу;
- инструкции активных skills, пока skill нужен задаче;
- нерешённые approvals;
- незавершённые tool calls и их идентификаторы;
- текущие hard limits;
- точные пути изменённых файлов и ссылки на созданные artifacts;
- последние сообщения, без которых следующий шаг теряет непосредственный смысл.
- provenance и revision извлечённых memory records, влияющих на текущее решение;
- parent/child run contracts и ещё не проверенные child results.

Если pinned data вместе с `output_reserve` не помещается в окно, запуск MUST завершиться с `CONTEXT_UNRECOVERABLE`. Ядро MUST NOT молча обрезать pinned data.

## Что суммаризируется

Кандидаты по порядку:

1. завершённые tool outputs;
2. старые промежуточные объяснения модели;
3. завершённые ветви исследования;
4. предыдущая summary вместе с новым отрезком истории.
5. неактуальные retrieved memories и tool schemas, не нужные следующему шагу.

Полные outputs остаются в artifact store и audit transcript. В активном контексте остаются ссылка, тип, размер, digest и краткое релевантное содержание.

## Формат summary

Summary MUST быть структурированной и содержать:

```text
Goal: исходная цель без переинтерпретации
Constraints: действующие ограничения и approvals
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
3. Построить новую summary с учётом предыдущей.
4. Проверить наличие pinned facts и структурных секций.
5. Пересчитать токены.
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

Memory retrieval имеет отдельный budget и выполняется до финального occupancy check. Retrieved item MAY быть вытеснен из active context без удаления из MemoryStore. Удалённая/tombstoned memory MUST быть немедленно исключена из новых context assemblies и очищена из caches.

## Проверка качества summary

Перед commit Context Engine выполняет structural checks и, для high-risk runs, MAY использовать независимый verifier model. Verifier проверяет только сохранность goal, constraints, facts, open side effects и provenance; он не заменяет summary новым планом.

## Проверяемые гарантии

- Исходный prompt побайтно совпадает до и после любого числа compaction.
- Ожидающий approval остаётся видимым и не исполняется из-за compaction.
- Ссылки на изменённые файлы и artifacts не теряются.
- После compaction runtime может объяснить, какие sequence ranges были заменены.
- Полный audit transcript не изменяется при compaction.
