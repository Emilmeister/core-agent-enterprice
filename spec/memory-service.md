# Память агента

## Граница подсистемы

Память является подсистемой Core Agent, а не отдельным сервисом. Markdown corpus, BM25/vector indexes, entity graph, NER и reranker живут внутри процесса агента, а модель обращается к ним через built-in tools `core_memory_*`. Отдельного memory-процесса, memory-контейнера и memory-MCP не существует; MCP-роль `memory` не назначается ни одному серверу.

Память включается конфигурацией развёртывания:

1. `CORE_AGENT_MEMORY` задаёт feature mode `optional|required|disabled`;
2. `MEMORY_STORAGE_TYPE` выбирает backend;
3. AgentConfig фильтрует отдельные memory tools;
4. model использует только effective catalog.

При `CORE_AGENT_MEMORY=disabled` ни один `core_memory_*` tool не попадает в effective catalog, memory capability policy не добавляется в системные инструкции, а backend не создаётся. При `required` отсутствующий или неработоспособный backend является ошибкой старта, а не тихой деградацией.

Feature mode является строкой, поэтому gating по прefixу `core_memory_` MUST приводить её к булеву значению. Проверка на truthiness самой строки неверна: `"disabled"` — непустая строка и прошла бы как включённая capability.

## Source of truth

Markdown-документ является единственным каноническим содержимым памяти. Chunks, embeddings, BM25 postings, entity graph, resolutions и caches MUST быть полностью перестраиваемы из Markdown corpus и versioned model/config metadata.

Модель не имеет ни filesystem, ни SQL доступа к corpus. Любая mutation проходит `core_memory_*` tool и enforcement подсистемы.

Документ, который парсер отвергает при загрузке, MUST стоить только себя: он пропускается с warning, а остальной corpus остаётся доступным. Отказ загрузки целиком превращает одну испорченную строку — например написанную до появления правила валидации — в полную недоступность памяти пользователя, включая тот `delete`, которым её можно было бы починить.

## Scope

Каждый memory-документ принадлежит тройке `(app_name, user_id, memory_id)`, где `app_name` — имя агента из AgentConfig, `user_id` — identity текущего run (`anonymous`, если identity отсутствует). Тройка является первичным ключом во всех backends.

Внутри этой тройки документ дополнительно принадлежит namespace, который определяет видимость между сессиями:

| Значение аргумента `scope` | Namespace | Видимость |
|---|---|---|
| `user` (default) | `subject/<user_id>` | во всех сессиях этого пользователя |
| `session` | `session/<session_id>` | только в текущей сессии |

Namespace MUST выводиться runtime-ом из scope текущего run и MUST NOT приниматься от модели как произвольная строка. Модель выбирает только перечисление `user|session`; путь, `user_id` и `session_id` подставляет runtime. Это исключает чтение чужого namespace подбором аргумента.

Run без `session_id` не может писать и читать `session` scope: такой вызов отклоняется как `TOOL_ARGUMENT_INVALID`. Фоновая задача, запущенная через `core_task_start`, выполняется без run scope, поэтому её память принадлежит `anonymous`; это ограничение, а не дефект, и оно совпадает с поведением artifact tools.

Main agent и сабагент имеют общую память только когда delegation contract явно передал memory tools: сабагент наследует ту же тройку scope, поэтому явно делегированный `core_memory_search` видит память родителя. Если parent не делегировал ни одного memory tool, child работает без памяти. Working scratchpad и Core transcript не являются общей памятью.

## Built-in tools

Подсистема публикует ровно шесть tools. Они образуют полный цикл авторства; операции `move`, `history`, `index_status` и `entity_resolve` остаются методами подсистемы и MUST NOT публиковаться модели.

### `core_memory_search`

```
query: string, 1..1000            обязателен
scope: "user" | "session"         default "user"
kind:  string                     optional, точный фильтр
limit: integer, 1..50             default MEMORY_SEARCH_LIMIT
```

Возвращает:

```json
{
  "results": [
    {
      "memory_id": "mem_01J...",
      "title": "Database migration decisions",
      "kind": "decision",
      "revision": 17,
      "excerpt": "...",
      "scores": {"bm25": 8.2, "vector": 0.83, "graph": 0.71, "final": 0.91}
    }
  ],
  "degraded_channels": [{"channel": "vector", "reason": "embeddings not configured"}],
  "index_revision": 17
}
```

`revision` в результате является тем самым значением, которое `update`, `split` и `delete` требуют как `expected_revision`. Модель получает его из `search` или `read` и не угадывает.

### `core_memory_read`

```
memory_id: string                 обязателен
```

Возвращает `memory_id`, `title`, `kind`, `status`, `scope`, `revision`, `body`, `body_line_count`.

### `core_memory_create`

```
title: string, 1..200             обязателен
body:  string                     обязателен, тело документа
kind:  string, 1..64              default "fact"
scope: "user" | "session"         default "user"
tags:  array of string            optional
```

Модель передаёт заголовок и тело, а не Markdown с front matter. Runtime сам формирует канонический документ: генерирует `memory_id`, подставляет `namespace`, `created_at`, `updated_at` и `sources` из текущего run, и складывает front matter. Требовать от модели корректный YAML — источник отказов, которые она не может диагностировать.

Возвращает `memory_id`, `revision`, `repository_revision`, `body_line_count`.

### `core_memory_update`

```
memory_id:         string         обязателен
body:              string         обязателен, полностью заменяет тело
expected_revision: integer        обязателен
title:             string         optional
status:            string         optional
```

`expected_revision` реализует optimistic concurrency. Расхождение возвращает `MEMORY_CONFLICT` с актуальным `current_revision`; last-write-wins запрещён.

### `core_memory_split`

```
memory_id:         string                                   обязателен
expected_revision: integer                                  обязателен
overview:          {title?: string, body: string}           обязателен
children:          [{title: string, body: string, kind?}]   обязателен, 1..20
```

Атомарно заменяет тело исходного документа на overview и создаёт дочерние документы. `memory_id` остаётся у overview. Каждый результирующий документ проходит ту же проверку 200 строк; превышение в любом из них отклоняет всю транзакцию целиком.

### `core_memory_delete`

```
memory_id:         string         обязателен
reason:            string         обязателен
expected_revision: integer        обязателен
```

Удаление исключает содержимое из Markdown, BM25, vectors, graph, summaries и caches в той же публикации.

## Ошибки, доступные модели

Ошибка memory tool MUST возвращаться модели как обычный failed tool result, а не завершать run. Это нормативное требование, а не деталь реализации: весь протокол 200 строк построен на том, что модель получает `MEMORY_FILE_TOO_LARGE` и отвечает на него вызовом `core_memory_split`. Run, упавший на этой ошибке, лишает протокол смысла.

Модели возвращаются как recoverable tool result:

| Код | Когда | Что модель делает дальше |
|---|---|---|
| `MEMORY_FILE_TOO_LARGE` | тело превысило 200 строк | вызывает `core_memory_split` с явным планом |
| `MEMORY_CONFLICT` | устаревший `expected_revision` или занятый путь | перечитывает документ и повторяет с актуальной revision |
| `MEMORY_INVALID` | аргументы не образуют корректный документ | исправляет аргументы |
| `NOT_FOUND` | неизвестный `memory_id` | ищет заново через `core_memory_search` |

Недоступность backend не является recoverable: она означает, что подсистема не может дать корректный ответ, и run завершается ошибкой. Отказ производных каналов — эмбеддингов и extraction — recoverable-ошибкой не является тем более: он вообще не доходит до модели как ошибка, потому что запись выполняется, а канал понижается.

## Формат Markdown

Каждый документ MUST иметь YAML front matter:

```yaml
---
id: mem_01J...
title: Database migration decisions
namespace: subject/user-42
kind: decision
status: active
created_at: 2026-07-11T10:00:00Z
updated_at: 2026-07-11T11:30:00Z
tags: [database, migration]
sources:
  - task_id: task_01J...
    event_revision: 42
---
```

Нормативные поля: immutable `id`, `title`, `namespace`, `kind`, `status`, timestamps и `sources`. Body использует headings, paragraphs, lists, tables и explicit links `[[mem_id]]`/`[[entity:canonical-id]]`.

Front matter построчен, поэтому значения, приходящие от модели, являются недоверенным вводом в этот формат. Runtime MUST отклонять `title`, `kind`, `status` и любой элемент `tags`, содержащий перевод строки или управляющий байт, кодом `MEMORY_INVALID`. Parser MUST отклонять повторяющийся ключ front matter тем же кодом. После разбора runtime MUST убедиться, что `id` и `namespace` совпадают с тем, что он сам подставил — для каждого документа, включая дочерние в `split`.

Определение перевода строки MUST совпадать с тем, которым пользуется парсер, а не перечисляться отдельно. Перечисление символов пропускает U+2028, U+2029 и U+0085: все три разрывают строку при разборе, но не являются управляющими байтами, и такой `title` завершает front matter досрочно — переписывая `kind`, `status` и timestamps существующей заметки и вынося настоящие строки заголовка в тело.

Элемент `tags` дополнительно MUST NOT содержать `,`, `[` и `]`: список хранится как `tags: [a, b]`, и эти символы не переживают обратное чтение — тег вернулся бы разбитым, а следующая запись сохранила бы уже испорченный список.

Три проверки существуют одновременно намеренно. `id` и `namespace` выводятся раньше полей модели, а разбор повторяющегося ключа по правилу «последний побеждает» превращает перевод строки в `title` в чужой `id`, а в `kind` — в чужой namespace. Первое переписывает существующую заметку без её revision, то есть ровно тот last-write-wins, который запрещён ниже; второе обходит изоляцию scope. Прямой источник такой строки — prompt injection из недоверенного содержимого, а не ошибка оператора.

`core_memory_update` и `core_memory_split` MUST сохранять `tags` и `sources` исходного документа: это нормативные поля, и обычная правка тела не является основанием терять provenance.

## Жёсткий лимит 200 строк

Committed документ MUST NOT иметь больше 200 body lines. YAML front matter и одна завершающая newline в лимит не входят.

`core_memory_create` или `core_memory_update`, результат которых содержит 201+ body lines, MUST:

1. отклонить операцию до canonical/index publication;
2. не изменить file/repository revision;
3. не обрезать content;
4. не выполнить автоматический split без нового явного tool call;
5. вернуть `MEMORY_FILE_TOO_LARGE` и рекомендацию разбить тему на несколько документов.

Пример результата отказа:

```json
{
  "code": "MEMORY_FILE_TOO_LARGE",
  "committed": false,
  "actual_body_lines": 237,
  "max_body_lines": 200,
  "recommended_action": "split_into_multiple_markdown_files",
  "suggested_boundaries": ["Overview", "Decisions", "Open questions"]
}
```

`suggested_boundaries` является рекомендацией, не mutation. Модель формирует split plan и отдельно вызывает `core_memory_split`.

Writer SHOULD начинать split при прогнозе 180+ строк, но подсистема обязана разрешать до 200 включительно.

## Когда искать, обновлять и создавать

Перед mutation модель MUST вызвать `core_memory_search` по теме, entities и предполагаемому title.

### Update

Использовать `core_memory_update`, когда найден тот же stable subject/topic, уточняется существующий факт/решение и новое тело остаётся в пределах 200 строк.

Противоречие не добавляется как вторая бесконтекстная истина: старый claim помечается superseded/uncertain или заменяется с history/provenance.

### Create

Использовать `core_memory_create`, когда:

- hybrid search не нашёл тот же topic после проверки top candidates;
- subject имеет самостоятельный lifecycle или scope;
- другой `kind` улучшает retrieval;
- oversized update требует отдельного дочернего документа;
- bounded event period закончился.

Другой wording или новый tag не являются основанием для duplicate.

### Split

`core_memory_split` принимает явный plan и атомарно создаёт overview/child документы. Каждый resulting документ также MUST пройти hard 200-line validation. Oversized child отклоняет всю transaction.

Split выполняется по heading/topic boundary, не посередине claim, table или code block. Stable ID остаётся у overview; aliases и links сохраняют provenance.

## Transactional indexing

При успешном create/update/split/delete подсистема:

1. проверяет scope, expected revision, schema, links и 200-line limit;
2. chunk-ит Markdown по headings/blocks со stable anchors;
3. удаляет derived data изменённых/удалённых chunks;
4. обновляет BM25;
5. вычисляет embeddings изменённых документов;
6. выполняет NER и relation extraction изменённых chunks;
7. выполняет entity resolution;
8. строит graph edges с provenance;
9. проходит consistency checks;
10. атомарно публикует одну corpus/index/graph revision через backend;
11. возвращает tool result и испускает OTel telemetry.

Write считается successful только после publication. Обычный search не видит staging revision. Старый committed revision остаётся доступным при failure.

Embedding изменённого документа вычисляется ровно один раз — в момент публикации — и сохраняется вместе с документом. Вычисление embedding каждого документа на каждый search недопустимо: при внешнем embedding endpoint это превращает один поиск в N сетевых вызовов.

## Backend хранилища

`MEMORY_STORAGE_TYPE` выбирает backend и принимает ровно два значения:

| Значение | Поведение |
|---|---|
| `in-memory` (default) | corpus и derived data живут в процессе и теряются при рестарте |
| `postgres` | corpus и revisions лежат в общей БД агента |

`CORE_AGENT_ENVIRONMENT=production` с `MEMORY_STORAGE_TYPE=in-memory` и включённой памятью MUST завершаться ошибкой конфигурации: тихая потеря долговременной памяти при рестарте недопустима в production.

Backend является интеграцией и не управляет доменной семантикой: лимит 200 строк, revision-модель, hybrid retrieval и graph одинаковы для обоих значений.

Пул, который подсистема открыла сама, MUST проходить ту же проверку схемы, что и общий пул агента: миграцию при `DATABASE_AUTO_MIGRATE=true`, иначе `verify_schema`. Без неё агент стартует здоровым и отвечает на readiness, а первый же memory tool падает на отсутствующей таблице — отказ, который старт был обязан обнаружить.

### Контракт backend

Backend реализует ровно две обязательные операции и одну опциональную:

- `load()` — вернуть все документы, resolutions, историю ревизий и текущий `repository_revision`;
- `publish(repository_revision, documents, resolutions, writes, deletes)` — атомарно опубликовать следующую repository revision; конфликт номера ревизии MUST возвращать `MEMORY_CONFLICT`;
- `vector_candidates(namespace, query_embedding, limit)` — optional; вернуть кандидатов, отсортированных по косинусной близости.

Backend без `vector_candidates` не теряет векторный канал: подсистема считает косинус в процессе по сохранённым embeddings.

### PostgreSQL

Пул выбирается в фиксированном порядке: заданный `MEMORY_POSTGRES_HOST` собирает отдельный DSN и переопределяет всё остальное; иначе используется общий пул агента; а если общего пула нет — подсистема открывает собственный по `DATABASE_URL` и закрывает его при остановке. Последний случай не является ошибкой конфигурации: долговременная память рядом с эфемерными сессиями (`SESSION_STORAGE_TYPE=in-memory`) — законное развёртывание, и отказ на том основании, что общий пул ещё не создан, противоречил бы заданному `DATABASE_URL`. Если не задано ни `DATABASE_URL`, ни `MEMORY_POSTGRES_HOST`, старт завершается ошибкой конфигурации с указанием обеих переменных.

Компоненты отдельного DSN:

| Переменная | Значение по умолчанию |
|---|---|
| `MEMORY_POSTGRES_PROTOCOL` | `postgresql` |
| `MEMORY_POSTGRES_USER` | — |
| `MEMORY_POSTGRES_PASSWORD` | — |
| `MEMORY_POSTGRES_HOST` | — |
| `MEMORY_POSTGRES_PORT` | `5432` |
| `MEMORY_POSTGRES_DATABASE` | — |

Схема создаётся миграцией вместе с остальной схемой агента и версионируется тем же `core_schema_migrations`:

```sql
CREATE TABLE core_memory_documents (
    app_name    text        NOT NULL,
    user_id     text        NOT NULL,
    memory_id   text        NOT NULL,
    namespace   text        NOT NULL,
    path        text        NOT NULL,
    content     text        NOT NULL,
    revision    integer     NOT NULL,
    embedding   real[],
    entities    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, memory_id)
);

CREATE TABLE core_memory_document_versions (
    app_name    text        NOT NULL,
    user_id     text        NOT NULL,
    memory_id   text        NOT NULL,
    revision    integer     NOT NULL,
    content     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, memory_id, revision)
);

CREATE TABLE core_memory_revisions (
    app_name            text        NOT NULL,
    user_id             text        NOT NULL,
    repository_revision integer     NOT NULL,
    resolutions         jsonb       NOT NULL,
    published_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, repository_revision)
);
```

`content` хранит Markdown целиком, включая front matter. Хранение только тела недопустимо: `kind`, `status`, `sources` и timestamps являются частью канонического документа и должны переживать рестарт без реконструкции.

`entities` хранит результат extraction и является кэшем, а не источником истины: он восстановим из `content` повторным извлечением. `NULL` означает «не извлечено» и отличается от пустого массива, означающего «извлечено, сущностей нет»; первое понижает graph-канал, второе нет.

Первичный ключ `core_memory_revisions` является механизмом optimistic concurrency между процессами: вторая реплика, публикующая ту же repository revision, получает нарушение уникальности и MUST превратить его в `MEMORY_CONFLICT`.

Публикация MUST выполняться одной транзакцией: запись версий, обновление текущих документов, удаление tombstoned строк и вставка revision-строки видны одновременно или не видны вовсе.

### pgvector

Embedding хранится в нативной колонке `real[]`, поэтому сохранение векторов не зависит от расширения и входит в обязательную миграцию. Расширение нужно только для индекса и оператора расстояния. Подсистема один раз при инициализации пытается выполнить вне транзакции обязательных миграций:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE INDEX IF NOT EXISTS core_memory_embedding_idx
  ON core_memory_documents USING hnsw ((embedding::vector(<EMBEDDING_DIMENSION>)) vector_cosine_ops)
  WHERE embedding IS NOT NULL;
```

Неуспех — например managed PostgreSQL без расширения `vector` — MUST логироваться и понижать векторный канал до вычисления косинуса в процессе по сохранённым `real[]`, но MUST NOT ронять ни миграцию, ни старт агента. Обязательные миграции не могут зависеть от наличия расширения: они выполняются одной транзакцией, и отказ на `CREATE EXTENSION` откатил бы всю схему.

Размерность фиксируется только в индексе, а не в колонке. Embedding, длина которого не совпадает с `EMBEDDING_DIMENSION`, сохраняется, но сопровождается warning: рассинхрон конфига не повод терять запись. Такой вектор не попадает в частичный индекс размерности `<EMBEDDING_DIMENSION>`, поэтому backend MUST отбрасывать кандидатов с несовпадающей длиной до обращения к оператору расстояния.

## Слой эмбеддингов

Слой включается, только если разрешены все три значения; иначе embedding отсутствует и поиск идёт текстом. Незаданный `EMBEDDING_API_BASE` берётся из `LLM_API_BASE`: развёртывания обычно отдают эмбеддинги и генерацию с одного OpenAI-совместимого шлюза, и требовать повторения того же адреса — это отдельный способ получить наполовину настроенный слой. Ключ такого вывода не имеет: `EMBEDDING_API_KEY` задаётся явно, потому что права на эмбеддинги могут отличаться от прав на генерацию.

| Переменная | Значение по умолчанию | Назначение |
|---|---|---|
| `EMBEDDING_MODEL` | — | модель эмбеддингов; префикс `hosted_vllm/` срезается |
| `EMBEDDING_API_BASE` | `LLM_API_BASE` | база OpenAI-совместимого API |
| `EMBEDDING_API_KEY` | — | авторизация |
| `EMBEDDING_DIMENSION` | `768` | ожидаемая размерность вектора |
| `MEMORY_SEARCH_LIMIT` | `10` | сколько записей возвращает поиск по умолчанию |
| `ENTITY_ID` | — | если задан, добавляет заголовок `X-Internal-Entity-ID` |

Запросы к embedding endpoint всегда несут заголовки `X-Title` (имя агента) и `X-Internal-Title: evo_ai_agents`, как и запросы к LLM.

Алгоритм генерации фиксирован:

1. пустой текст → embedding отсутствует;
2. текст обрезается до 8000 символов;
3. запрос идёт OpenAI-совместимым `POST {EMBEDDING_API_BASE}/embeddings` с `{model, input}`;
4. берётся `data[0].embedding`;
5. несовпадение длины с `EMBEDDING_DIMENSION` даёт warning, но вектор сохраняется;
6. любая ошибка — сеть, таймаут, авторизация, некорректная форма ответа — возвращает отсутствие embedding.

Ошибка embedding MUST NOT ронять tool call. Отсутствие embedding понижает векторный канал, и поиск продолжается по BM25 и graph. Текст запроса, ключ и тело ответа не попадают в public error, audit и telemetry.

## Entity graph и NER

Graph содержит минимум nodes `memory_file`, `chunk`, `entity`, `claim`, `task`, `artifact` и edges `contains`, `mentions`, `relates_to`, `supports`, `contradicts`, `supersedes`, `derived_from`, explicit Markdown links.

Каждая mention/relation хранит evidence chunk/line, source revision, extractor version, confidence и timestamp. Изменённый chunk удаляет stale edges до publication. Low-confidence entity merge остаётся candidate.

Extraction выполняется только для документов, содержимое которых изменилось. Повторный проход по всему corpus на каждую запись превращает один tool call в один сетевой запрос на каждую заметку — тот же отказ, который явно запрещён для эмбеддингов.

Extraction выполняет модель агента через structured output. Отдельного NER-сервиса нет: `MEMORY_NER_ENDPOINT`, `MEMORY_NER_MODEL` и `MEMORY_NER_API_KEY` не существуют. Слой включается, только если заданы `LLM_API_BASE`, `LLM_API_KEY` и `LLM_MODEL`, и использует ту же модель и тот же шлюз, что и агент: содержимое заметки в любом случае пришло из контекста этой модели, поэтому новой границы доверия здесь не появляется, а вторая конфигурация появилась бы.

Запрос идёт OpenAI-совместимым `POST {LLM_API_BASE}/chat/completions` с `temperature: 0` и полной JSON-схемой ответа в каждом запросе:

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "memory_entity_extraction",
    "strict": true,
    "schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["entities"],
      "properties": {
        "entities": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["text", "type", "confidence"],
            "properties": {
              "text": {"type": "string"},
              "type": {"enum": ["person", "organization", "location", "product", "technology", "event", "other"]},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            }
          }
        }
      }
    }
  }
}
```

Схема передаётся целиком, а не по имени зарегистрированного шаблона: подсистема не может знать, какие схемы шлюз хранит, а запрос со схемой работает на любом OpenAI-совместимом шлюзе с guided decoding.

Заданный `LLM_ENDPOINT` полностью заменяет производный адрес по тому же правилу, что и для основного клиента модели. Формат `anthropic` не имеет `response_format`, поэтому при `LLM_API_FORMAT=anthropic` слой выключен и сообщает об этом в `startup.configuration`: отказ, видимый при старте, дешевле отказа, обнаруженного первой записью в память.

Модель возвращает текст сущности, тип и confidence, но не offsets. Смещения вычисляет подсистема поиском подстроки в исходном тексте: это единственная часть ответа, которую модель систематически выдумывает, и та же проверка даёт grounding. Сущность, отсутствующая в тексте дословно, MUST отбрасываться — галлюцинацию отсекают, а не исправляют. Ответ MUST валидироваться подсистемой независимо от `strict`: schema enforcement — свойство шлюза, а не гарантия вызывающего.

Результат extraction MUST сохраняться вместе с документом. Извлечение при загрузке corpus превращает каждый холодный старт в один запрос к модели на каждую заметку, а на платформе со scale-to-zero холодный старт — норма. Документ без сохранённых сущностей индексируется в процессе встроенным regex-экстрактором.

Отказ extraction при записи MUST NOT отменять запись. BM25 и векторный канал от extraction не зависят, поэтому документ остаётся полностью находимым, и отмена записи обменяла бы потерю данных на качество одного из трёх каналов. Документ публикуется без сохранённых сущностей, а graph-канал помечается degraded с числом таких документов — числом, а не именами: перечень заметок является содержимым памяти.

Постоянный отказ шлюза — 4xx, кроме 408 и 429 — MUST выключать extraction до конца жизни процесса. Ответ `400` на неподдерживаемый `response_format` не станет успехом без изменения конфигурации, а один заведомо неуспешный запрос к модели на каждую запись памяти — это цена без выигрыша.

Chunker, BM25 config, embedding model, NER/relation extractor, taxonomy и resolution thresholds входят в index revision.

## Hybrid retrieval и rerank

Candidate generation параллельно получает:

- BM25 candidates для exact terms/identifiers;
- vector candidates для semantic similarity — из backend, если он реализует `vector_candidates`, иначе косинусом по сохранённым embeddings;
- graph candidates через query NER/entity linking и bounded typed traversal.

Сопоставление query с сущностями MUST выполняться по токенам, а не по строке сущности целиком. Извлечённая моделью сущность обычно многословна — «Cloud.ru ML Inference», «Эмиль Мадатов», — и сравнение её со строкой запроса даёт совпадение только при дословном повторении всей фразы, то есть graph-канал молча перестаёт работать ровно тогда, когда extraction начинает работать хорошо.

Токенизация BM25 и извлечение сущностей MUST быть независимы от письменности. Класс символов, ограниченный латиницей, обнуляет запрос и документ целиком на любом другом алфавите: BM25 получает ноль, а без настроенного слоя эмбеддингов обнуляются все каналы сразу, и поиск возвращает пустой результат при полностью корректно сохранённой заметке. Это отказ, неотличимый для оператора от «ничего не найдено».

Candidates объединяются по stable ID. Несопоставимые raw scores не складываются напрямую; используется calibrated normalization или Reciprocal Rank Fusion.

Reranker получает query, text/heading, BM25/vector ranks/scores, graph paths/features, namespace, recency, confidence и provenance. Result возвращает component scores, final score, index revision и reason codes.

Недоступный channel явно помечает degraded search. Отсутствие embedding-конфигурации является именно degraded search, а не отказом: поиск всегда возвращает результат по оставшимся каналам. Candidate set и model/index/reranker versions сохраняются для reproducibility. Conflicting claims возвращаются вместе, а не скрываются ranking-ом.

## Concurrency

- Внутри процесса mutations сериализуются одним lock-ом.
- Между процессами writers используют optimistic concurrency по file/repository revision.
- Last-write-wins запрещён.
- Conflict возвращает revision, на которой backend находится фактически, а не ту, которую предлагал проигравший writer. Вычислять её из собственного номера бесполезно: получится ровно то устаревшее значение, с которого writer и начал.
- Получив conflict от backend, подсистема MUST перечитать состояние до того, как вернуть ошибку. Иначе она продолжит предлагать тот же номер, а retry, который эта же ошибка предписывает модели, не сможет выполниться никогда.
- `load()` MUST NOT возвращать номер revision новее, чем прочитанный им corpus. Чтение по одному соединению без общей транзакции даёт каждому запросу собственный snapshot, поэтому номер revision читается до документов: отставший номер приводит к корректному конфликту при следующей публикации, а опережающий — к молчаливому удалению чужой записи.
- Publication получает monotonic repository revision в пределах scope `(app_name, user_id)`.

## Security и retention

- Подсистема выводит namespace из authenticated run scope и никогда не принимает его от модели.
- Secrets, raw credentials, hidden reasoning и запрещённые tool outputs не записываются.
- Delete инвалидирует Markdown, BM25, vectors, graph, summaries и caches.
- Core Agent хранит только необходимые tool results в task transcript/context согласно своей retention policy.

## OpenTelemetry

Подсистема создаёт spans под префиксом `core_agent.memory.`: `core_agent.memory.search` и её дочерние `.bm25`, `.vector`, `.graph`, `.rerank`, а также `core_agent.memory.ner`, `core_agent.memory.embed` и `core_agent.memory.index_publish`. Префикс `memory_service.` больше не используется: процесса с таким именем не существует.

Span kind выводится из имени: `core_agent.memory.search*` — `RETRIEVER`, `*.rerank` — `RERANKER`, `*.embed` — `EMBEDDING`.

Memory content, query text и entity names выключены в telemetry по умолчанию. Деградация каналов наблюдаема как атрибут search span и как `degraded_channels` в tool result.
