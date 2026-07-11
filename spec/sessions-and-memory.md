# Sessions и Markdown memory

## Модель состояния

- **Session** — A2A `contextId`, объединяющий связанные Tasks/Messages.
- **Run/Task** — одно выполнение prompt или фоновая работа.
- **Transcript** — immutable последовательность Messages, model, tool и control events.
- **Working state** — goal, plan, constraints, pending tasks/approvals и artifacts активного run.
- **Memory** — versioned Markdown corpus, общий для main agent и разрешённых сабагентов.
- **Derived indexes** — BM25, vector embeddings и entity graph для retrieval.

Transcript не равен memory. Наличие текста в истории не делает его автоматически долгосрочным фактом.

## Source of truth

Markdown files являются единственным каноническим содержимым memory. Graph, chunks, embeddings, BM25 postings, caches и summaries MUST быть полностью перестраиваемы из Markdown corpus и versioned model/config metadata.

Agent и сабагенты MUST изменять memory только через `core.memory.*` tools. Прямой terminal/filesystem write в memory root блокируется ExecutionEnvironment и policy, даже если AgentProfilePrompt просит обратное.

## Scopes и layout

```text
memory/
├── session/<context-id>/
├── subject/<subject-id>/
├── tenant/
└── shared/
```

- `session` доступен tasks одной session;
- `subject` переносится между sessions с consent/policy;
- `tenant` хранит организационные решения и знания;
- `shared` содержит явно опубликованные non-tenant product facts.

Scope выбирает policy/tool, не произвольный путь модели. Cross-scope link разрешён только при совместимых ACL. Main agent и его children по умолчанию разделяют session scope и одну revision stream; delegation contract MAY сузить read/write.

## Формат Markdown файла

Каждый файл MUST иметь YAML front matter:

```yaml
---
id: mem_01J...
title: Database migration decisions
scope: session
kind: decision
status: active
created_at: 2026-07-11T10:00:00Z
updated_at: 2026-07-11T11:30:00Z
tags: [database, migration]
sources:
  - run_id: run_01J...
    sequence: 42
---
```

Нормативные поля: immutable `id`, `title`, `scope`, `kind`, `status`, timestamps и `sources`. `kind` минимум поддерживает `fact`, `preference`, `decision`, `procedure`, `project`, `event`, `summary` и `index`.

Body использует обычные headings, paragraphs, lists, tables и explicit wiki links `[[mem_id]]` или `[[entity:canonical-id]]`. Произвольный executable content не исполняется.

## Гранулярность и лимит файла

- Один файл описывает одну cohesive тему, entity, decision family или bounded period.
- Файл MUST NOT превышать 200 строк, не считая YAML front matter и одной завершающей newline.
- Writer SHOULD начинать split при прогнозе 180+ строк, чтобы небольшое обновление не создавало немедленный повторный split.
- Большая тема представляется коротким `kind: index` overview и дочерними topical files.
- Split выполняется по heading/topic boundary, не посередине claims/table/code block.
- Старый stable `id` остаётся у overview или наиболее прямого successor; aliases/links сохраняют навигацию и graph provenance.

Лимит 200 строк относится к committed revision. Tool MUST отклонить update, создающий 201+ строк, и предложить atomic split plan.

## Когда искать, обновлять и создавать

Перед любой записью agent MUST выполнить `core.memory.search` по теме, ключевым entities и предполагаемому title.

### Обновить существующий файл

Использовать `core.memory.update`, когда:

- найден файл с тем же stable subject/topic;
- уточняется или исправляется существующий факт/решение;
- добавляется новое состояние той же сущности;
- итог после изменения остаётся не более 200 строк;
- provenance нового утверждения можно указать явно.

Противоречие не добавляется рядом как ещё одна «истина». Старый claim помечается superseded/uncertain или заменяется с сохранением history/provenance.

### Создать новый файл

Использовать `core.memory.create`, когда:

- hybrid search не нашёл файл той же темы после проверки top candidates;
- новый subject имеет самостоятельный lifecycle или ACL/scope;
- новая запись относится к другому `kind` и совместное хранение ухудшает retrieval;
- update превысил бы 200 строк — тогда create является частью `memory.split`;
- periodized event log перешёл в новый bounded period.

Создание файла только из-за другого wording или нового тега запрещено. Duplicate topic MUST быть объединён или связан, а не размножен.

### Split, move и delete

- `core.memory.split` атомарно создаёт overview/children, переносит content и обновляет links.
- `core.memory.move` меняет path/title без смены `id`.
- `core.memory.delete` создаёт tombstone и удаляет content из новых retrieval revisions.
- Факт, который необходимо забыть, нельзя оставлять в summary/index/embedding cache.
- Merge дубликатов сохраняет aliases и provenance всех predecessors.

## Memory tools

Ядро MUST предоставлять:

- `core.memory.search(query, scope, filters, limit)` — hybrid retrieval с component/final scores;
- `core.memory.read(id_or_path, revision, section)` — точное чтение Markdown/section;
- `core.memory.create(path, content, expected_repository_revision)`;
- `core.memory.update(id, patch, expected_file_revision)`;
- `core.memory.split(id, plan, expected_file_revision)`;
- `core.memory.move(id, new_path, expected_file_revision)`;
- `core.memory.delete(id, reason, expected_file_revision)`;
- `core.memory.history(id)` — revisions/provenance/tombstones;
- `core.memory.index_status(revision)` — состояние chunk/BM25/vector/NER/graph publication;
- `core.memory.entity_resolve(entity_id, target_id, evidence)` — исправление entity resolution через auditable Markdown metadata/update.

Create/update/split/move/delete MUST возвращать committed memory revision, changed IDs и index revision. Write считается успешным только после atomic publication канонических files и derived indexes либо возвращает background Task handle для bulk operation; непроиндексированный draft не виден обычному search.

## Transactional indexing pipeline

При каждом create/update/split/move/delete MemoryStore MUST:

1. проверить ACL, expected revision, schema, links и 200-line limit;
2. записать изменения в staging revision;
3. разобрать Markdown по headings, blocks и stable chunk anchors;
4. удалить derived records изменённых/удалённых chunks;
5. обновить BM25 postings;
6. вычислить embeddings изменённых chunks;
7. выполнить NER и relation extraction для изменённых chunks;
8. выполнить entity resolution против canonical graph;
9. построить mention/relation/link edges с provenance;
10. пройти consistency checks;
11. атомарно опубликовать одну corpus/index/graph revision;
12. испустить `memory.updated` и OTel telemetry.

Неизменённые chunks сохраняют stable IDs, embeddings и graph edges. Изменённый chunk получает новую content revision; stale entity mentions/relations MUST быть удалены до publication.

Если embeddings или NER временно недоступны, write не публикует частично согласованную revision. Он MAY остаться staging background Task и retry-иться по policy. После terminal failure старый committed revision остаётся читаемым.

## Chunking

- Chunk boundary следует Markdown heading/paragraph/list/table/code structure.
- Chunk хранит `memory_id`, heading path, ordinal, content digest, source revision и line range.
- Chunk не должен терять title/heading context при embedding или rerank.
- Маленькие соседние blocks MAY объединяться; большие section MUST делиться по semantic block boundary.
- Изменение одного section не должно перенумеровывать stable anchors остальных sections без необходимости.

## Entity graph и NER

Graph содержит минимум:

- nodes: `memory_file`, `chunk`, `entity`, `claim`, `task`, `artifact`;
- edges: `contains`, `mentions`, `relates_to`, `supports`, `contradicts`, `supersedes`, `derived_from`, explicit Markdown links;
- provenance: memory/chunk revision, line range, extractor version, confidence и timestamp.

NER pipeline извлекает именованные entities и нормализованные types; relation extraction создаёт только edges с evidence span. Entity resolution связывает mention с существующим canonical entity либо создаёт candidate node. Low-confidence merge остаётся candidate и не сливает nodes автоматически.

Chunker, BM25 config, embedding model, NER/relation extractor, taxonomy и resolution thresholds фиксируются в index revision. Их изменение запускает контролируемый full reindex в фоне; старая revision обслуживает search до атомарного переключения.

Ручная коррекция entity merge/split записывается как каноническое memory metadata/decision и имеет приоритет над следующими автоматическими NER runs.

## Hybrid retrieval

Search состоит из двух стадий.

### Candidate generation

Параллельно формируются:

- BM25 candidates по exact terms, identifiers и rare tokens;
- vector candidates по embeddings semantic similarity;
- graph candidates от query entities через bounded typed traversal.

Query NER/entity linking выполняется до graph traversal. Graph expansion имеет ограничение hops, edge types, ACL и candidate budget. Каждый retriever возвращает score, rank и provenance.

Candidates объединяются по stable chunk ID. Несопоставимые raw scores MUST NOT складываться напрямую; используется calibrated normalization или Reciprocal Rank Fusion с versioned parameters.

### Rerank

Reranker получает query, candidate text/heading, BM25/vector ranks/scores, graph paths/features, scope, recency, confidence и provenance. Он возвращает единый `final_score` и reason codes. Final order MUST учитывать все три канала, а не заменять hybrid search одним embedding score.

Результат search содержит:

```json
{
  "memory_id": "mem_01J...",
  "chunk_id": "chunk_...",
  "revision": 17,
  "scores": {"bm25": 8.2, "vector": 0.83, "graph": 0.71, "final": 0.91},
  "graph_paths": [["entity:a", "supports", "claim:b"]],
  "provenance": {"path": "session/project.md", "heading": "Decisions"}
}
```

Missing component is marked unavailable with cause; search MAY degrade by policy but MUST NOT present degraded result as full hybrid. Retrieval trace records model/index/reranker versions and candidate sets for reproducibility.

## Freshness, conflicts и claims

- Новое явное утверждение пользователя может supersede старое, но history сохраняется до retention expiry.
- Conflicting active claims возвращаются вместе с provenance; reranker не скрывает конфликт более высоким score.
- Внешние факты с freshness requirement получают `valid_at`/`expires_at` и перепроверяются.
- Model inference без source сохраняется только как `kind: summary`/assumption, не как confirmed fact.
- Secrets, credentials, raw private tool output и hidden reasoning MUST NOT попадать в memory.

## Concurrency и общая память сабагентов

- Все writers используют optimistic concurrency по file и repository revision.
- Last-write-wins запрещён.
- Независимые files MAY индексироваться параллельно, но publication получает один monotonic repository revision.
- Conflict возвращает current revision/diff metadata; agent обязан перечитать и повторить semantic merge.
- Main agent и сабагенты получают `memory.updated` через durable task mailbox.
- Active model context не меняется посередине turn; новая memory revision доступна на следующей safe boundary.

## Пользовательское управление и retention

Авторизованный пользователь может увидеть, почему файл/claim сохранён и использован, исправить его, экспортировать scope, отключить дальнейшую запись и удалить file/session/subject memory.

Delete MUST инвалидировать Markdown content, BM25 postings, vectors, graph nodes/edges, summaries и caches в пределах SLA. Audit MAY сохранить минимальный policy-required tombstone без удалённого content.

## Session lifecycle

Session имеет состояния `active`, `closed`, `archived`, `deleted`. Closed session не принимает новые Messages, но её memory остаётся под retention policy. Deleted session не восстанавливается через обычный API.

Concurrent A2A Tasks в одной session используют optimistic concurrency по session/memory revisions и никогда молча не перезаписывают memory друг друга.
