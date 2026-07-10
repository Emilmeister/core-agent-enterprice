# Sessions и memory

## Модель состояния

- **Session** — долговременный контекст взаимодействия пользователя с агентом.
- **Run** — одно выполнение одного prompt внутри session или без неё.
- **Turn** — один model call и последующая обработка запрошенных tools.
- **Transcript** — неизменяемая последовательность пользовательских, модельных, tool и control events.
- **Memory** — отдельно управляемые факты и summaries, которые MAY быть извлечены в будущий контекст.

Transcript не равен memory: то, что произошло, не обязано автоматически становиться долгосрочной инструкцией или фактом о пользователе.

## Создание и адресация session

Transport предоставляет операции создать, получить, закрыть, экспортировать и удалить session. RunRequest не содержит `session_id`: adapter связывает запрос с session через resource path, object handle или execution context.

Если session не указана, ядро создаёт ephemeral session на время run. Это сохраняет одинаковую внутреннюю модель для stateless и stateful режимов.

## Слои памяти

### Active context

Данные конкретного следующего model call. Ограничены окном модели и пересобираются Context Engine.

### Working state

Структурированные goal, plan, constraints, unresolved actions, artifacts и summaries текущего run. Durable и не зависит от того, помещается ли всё в model context.

### Session memory

Подтверждённые факты, предпочтения, решения и незавершённые темы, полезные следующим runs этой session.

### User/tenant memory

Опциональная память между sessions. По умолчанию отключена до явной host policy и user consent. Всегда scoped по tenant и subject identity.

### External knowledge

Документы и записи из MCP, search или host retrieval. Хранят provenance и freshness, но не считаются фактом о пользователе.

## Memory record

```json
{
  "memory_id": "mem_01...",
  "scope": "session",
  "kind": "preference",
  "content": "Пользователь предпочитает ответы на русском",
  "provenance": [{"run_id": "run_01...", "sequence": 42}],
  "confidence": 0.98,
  "created_at": "2026-07-10T19:30:00Z",
  "valid_from": "2026-07-10T19:30:00Z",
  "expires_at": null,
  "status": "active"
}
```

Record MUST иметь scope, kind, provenance, timestamps, status и content digest. Confidence помогает retrieval, но не заменяет provenance.

## Запись памяти

- Working state обновляется автоматически как часть checkpoint.
- Session memory MAY записываться автоматически для фактов, явно данных пользователем, и решений внутри session.
- User/tenant memory требует разрешающей host policy и наблюдаемого пользователю поведения.
- Чувствительные данные, secrets, одноразовые tokens и raw tool outputs MUST NOT становиться memory.
- Вывод модели без подтверждающего source не должен записываться как факт.
- Memory mutation фиксируется событием и поддерживает исправление/tombstone.

## Retrieval

Перед model call Context Engine:

1. формирует query из активной цели и entities;
2. применяет tenant/session/subject ACL;
3. фильтрует expired, tombstoned и policy-forbidden records;
4. ранжирует по relevance, recency, confidence и kind;
5. выбирает записи в пределах отдельного memory token budget;
6. добавляет provenance и явно маркирует memory как retrieved context.

Retrieval MUST быть детерминированно воспроизводим по сохранённым candidates, scores и policy version, даже если сам ranking model изменился позднее.

## Конфликты и устаревание

- Более новое явное утверждение пользователя заменяет старое, но старое остаётся tombstoned в аудите до retention expiry.
- Конфликтующие активные records не склеиваются в ложный компромисс; модель получает конфликт с provenance либо просит уточнение.
- Внешние данные с freshness requirement должны перепроверяться, а не извлекаться как вечный факт.
- Policy MAY задавать TTL по kind.

## Управление пользователем

Продукт MUST позволять авторизованному пользователю:

- увидеть, почему memory сохранена и где использовалась;
- исправить запись;
- удалить запись, session или всю субъектную memory;
- отключить дальнейшее сохранение;
- экспортировать transcript и memory в машиночитаемом формате.

Удаление распространяется на search indexes и caches в пределах документированного SLA. Audit MAY сохранить минимальный tombstone, если этого требует закон или security policy.

## Session lifecycle

Session имеет состояния `active`, `closed`, `archived`, `deleted`. Закрытая session не принимает новые runs, но может быть открыта снова policy-разрешённой операцией. Удалённая session не восстанавливается через обычный API.

Concurrent runs в одной session используют optimistic concurrency по session revision. Memory commit от проигравшего writer-а должен быть повторно сверен, а не молча перезаписать новые данные.
