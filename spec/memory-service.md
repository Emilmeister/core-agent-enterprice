# Memory MCP Service

## Граница сервиса

Memory — отдельный сервис с MCP interface. Core Agent не содержит MemoryStore, Markdown filesystem, BM25/vector indexes, graph, NER или reranker. Он видит только разрешённые MCP tools/resources Memory Service.

Memory включается так же, как любая другая внешняя capability:

1. AgentConfig разрешает `features.memory: optional|required`;
2. A2A Task передаёт MCP descriptor с host-validated role `memory`;
3. Core выполняет MCP initialize/discovery;
4. AgentConfig фильтрует memory tools;
5. model использует только effective catalog.

При `features.memory: disabled` memory MCP и его instructions/tools отсутствуют. Core Agent не выполняет скрытый fallback на локальную память.

## Source of truth

Markdown files являются единственным каноническим содержимым Memory Service. Graph, chunks, embeddings, BM25 postings, caches и summaries MUST быть полностью перестраиваемы из Markdown corpus и versioned model/config metadata.

Agent не имеет прямого filesystem access к memory corpus. Любая mutation проходит MCP tool и enforcement самого Memory Service.

## MCP capability profile

Memory Service объявляет stable server identity, protocol/profile version и tools:

- `memory.search(query, namespace, filters, limit)`;
- `memory.read(id_or_path, revision, section)`;
- `memory.create(path, content, expected_repository_revision)`;
- `memory.update(id, patch, expected_file_revision)`;
- `memory.split(id, plan, expected_file_revision)`;
- `memory.move(id, new_path, expected_file_revision)`;
- `memory.delete(id, reason, expected_file_revision)`;
- `memory.history(id)`;
- `memory.index_status(revision)`;
- `memory.entity_resolve(entity_id, target_id, evidence)`.

AgentConfig MAY убрать любой memory tool. Например read-only agent получает только `search`, `read`, `history`; отключённая mutation не показывается модели.

Memory-specific authoring instructions поставляются как versioned trusted capability policy profile, связанный с подтверждённой identity Memory MCP. AgentProfilePrompt не может их отменить. Критические ограничения, включая 200-line limit, в любом случае enforced service-ом, а не только инструкцией модели.

## Namespaces и общая память

```text
session/<a2a-context-id>/
subject/<subject-id>/
tenant/<tenant-id>/
shared/
```

Namespace выбирается authenticated MCP context/policy, не произвольным path argument.

Main agent и сабагент имеют общую память только если delegation contract явно передал:

- тот же Memory MCP server identity;
- тот же namespace/access token scope;
- конкретный allowlist memory tools.

Working scratchpad или Core transcript не являются общей памятью. Если parent не передал Memory MCP, child работает без memory.

## Формат Markdown

Каждый файл MUST иметь YAML front matter:

```yaml
---
id: mem_01J...
title: Database migration decisions
namespace: session/context_01J...
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

## Жёсткий лимит 200 строк

Committed Markdown file MUST NOT иметь больше 200 body lines. YAML front matter и одна завершающая newline в лимит не входят.

`memory.create` или `memory.update`, результат которых содержит 201+ body lines, MUST:

1. отклонить операцию до canonical/index publication;
2. не изменить file/repository revision;
3. не обрезать content;
4. не выполнить автоматический split без нового явного tool call;
5. вернуть `MEMORY_FILE_TOO_LARGE` и рекомендацию разбить тему на несколько Markdown files.

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

`suggested_boundaries` является рекомендацией, не mutation. Agent должен сформировать split plan и отдельно вызвать `memory.split` либо несколько create/update calls.

Writer SHOULD начинать split при прогнозе 180+ строк, но service обязан разрешать до 200 включительно.

## Когда искать, обновлять и создавать

Перед mutation agent MUST вызвать `memory.search` по теме, entities и предполагаемому title.

### Update

Использовать `memory.update`, когда найден тот же stable subject/topic, уточняется существующий факт/решение и новый body остаётся в пределах 200 строк.

Противоречие не добавляется как вторая бесконтекстная истина: старый claim помечается superseded/uncertain или заменяется с history/provenance.

### Create

Использовать `memory.create`, когда:

- hybrid search не нашёл тот же topic после проверки top candidates;
- subject имеет самостоятельный lifecycle или ACL/namespace;
- другой `kind` улучшает retrieval;
- oversized update требует отдельного дочернего файла;
- bounded event period закончился.

Другой wording или новый tag не являются основанием для duplicate file.

### Split

`memory.split` принимает явный plan и атомарно создаёт overview/child files. Каждый resulting file также MUST пройти hard 200-line validation. Oversized child отклоняет всю transaction.

Split выполняется по heading/topic boundary, не посередине claim, table или code block. Stable ID остаётся у overview/главного successor; aliases и links сохраняют provenance.

## Transactional indexing

При успешном create/update/split/move/delete Memory Service:

1. проверяет ACL, expected revision, schema, links и 200-line limit;
2. записывает staging revision;
3. chunk-ит Markdown по headings/blocks со stable anchors;
4. удаляет derived data изменённых/удалённых chunks;
5. обновляет BM25;
6. вычисляет embeddings;
7. выполняет NER и relation extraction изменённых chunks;
8. выполняет entity resolution;
9. строит graph edges с provenance;
10. проходит consistency checks;
11. атомарно публикует одну corpus/index/graph revision;
12. возвращает MCP result и испускает OTel telemetry.

Write считается successful только после publication либо возвращает background task handle для bulk operation. Обычный search не видит staging revision. Старый committed revision остаётся доступным при failure.

## Entity graph и NER

Graph содержит минимум nodes `memory_file`, `chunk`, `entity`, `claim`, `task`, `artifact` и edges `contains`, `mentions`, `relates_to`, `supports`, `contradicts`, `supersedes`, `derived_from`, explicit Markdown links.

Каждая mention/relation хранит evidence chunk/line, source revision, extractor version, confidence и timestamp. Изменённый chunk удаляет stale edges до publication. Low-confidence entity merge остаётся candidate.

Chunker, BM25 config, embedding model, NER/relation extractor, taxonomy и resolution thresholds входят в index revision. Их изменение создаёт full reindex background job; старая revision обслуживает search до atomic switch.

## Hybrid retrieval и rerank

Candidate generation параллельно получает:

- BM25 candidates для exact terms/identifiers;
- vector candidates для semantic similarity;
- graph candidates через query NER/entity linking и bounded typed traversal.

Candidates объединяются по stable chunk ID. Несопоставимые raw scores не складываются напрямую; используется calibrated normalization или Reciprocal Rank Fusion.

Reranker получает query, text/heading, BM25/vector ranks/scores, graph paths/features, namespace, recency, confidence и provenance. Result возвращает component scores, final score, index revision и reason codes.

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

Missing channel явно помечает degraded search. Candidate set и model/index/reranker versions сохраняются для reproducibility. Conflicting claims возвращаются вместе, а не скрываются ranking-ом.

## Concurrency

- Writers используют optimistic concurrency по file/repository revision.
- Last-write-wins запрещён.
- Conflict возвращает current revision/diff metadata.
- Независимые files MAY индексироваться параллельно, publication получает monotonic repository revision.
- Service notification новой revision доставляется через MCP capability/host event bridge; Core добавляет её на следующей safe boundary.

## Security и retention

- Memory Service самостоятельно проверяет tenant/namespace ACL до поиска или раскрытия существования ID.
- Secrets, raw credentials, hidden reasoning и запрещённые tool outputs не записываются.
- Delete/tombstone инвалидирует Markdown, BM25, vectors, graph, summaries и caches.
- Export/delete/consent policy принадлежит Memory Service и tenant control plane.
- Core Agent хранит только необходимые MCP results в task transcript/context согласно своей retention policy.

## OpenTelemetry

Memory Service MUST экспортировать OTel traces, metrics и logs через OTLP. Core MCP client передаёт W3C Trace Context; service создаёт spans для search, BM25, vector, graph, rerank, write validation, chunking, embeddings, NER, entity resolution и atomic publication.

Memory content, query text и entity names выключены в telemetry по умолчанию. Memory service outage/degradation наблюдаемы как MCP outcome и не заставляют Core Agent незаметно перейти на другую память.

## Production provider configuration

Production process (`MEMORY_ENVIRONMENT=production`) MUST получить `MEMORY_ROOT`, непустой allowlist `MEMORY_ALLOWED_NAMESPACE_PREFIXES`, `MEMORY_EMBEDDING_ENDPOINT`/`MEMORY_EMBEDDING_MODEL` и `MEMORY_NER_ENDPOINT`/`MEMORY_NER_MODEL` из deployment config. API credentials передаются отдельно через `MEMORY_EMBEDDING_API_KEY` и `MEMORY_NER_API_KEY`, если endpoint требует authentication. Built-in hash embeddings и regex proper-name extractor являются только development/test adapters; production startup с ними запрещён.

Embedding endpoint использует OpenAI-compatible embeddings request/response. NER endpoint принимает typed JSON `{model, text}` и возвращает bounded `{entities, relations}` с offsets, types и confidence. Оба adapter имеют timeout и response-size limit, валидируют shape/numeric values и не включают input text или credentials в public error, audit или telemetry.
