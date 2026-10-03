# Публичный контракт

Основной внешний protocol — [A2A](a2a-protocol.md). Этот документ определяет Core Agent payload и semantics поверх A2A, а не отдельный HTTP/RPC protocol.

## Логический RunRequest

Внутреннее ядро получает ровно один пользовательский вход:

```json
{"prompt": "Исправь падающий тест и объясни причину"}
```

A2A adapter строит его из `Message.parts`. Никакого собственного расширения протокола для этого не требуется: любой стандартный A2A-клиент является валидным клиентом Core Agent.

MCP-серверы и skills MUST задаваться конфигурацией развёртывания и MUST NOT приниматься из запроса. Прежняя передача их через versioned extension удалена: она делала обязательным собственное расширение на каждом запросе и тем самым отсекала стандартные A2A-клиенты, не давая взамен ничего, что нельзя выразить конфигурацией.

Session/tenant/auth/trace/task delivery принадлежат A2A context и transport security. Они не становятся вторым полем RunRequest.

## `prompt`

- Непустой text Part является обязательным shorthand.
- Целевой контракт принимает A2A text, file/artifact reference и structured data Parts согласно advertised media types.
- Файлы передаются стандартными file Parts или поддерживаемыми авторизованными transport references; inline bytes допустимы, лимит считается по декодированным байтам.
- Message содержит один новый пользовательский turn. История A2A context/session добавляется ядром.
- Исходные Parts, role, IDs и digests сохраняются на протяжении Task.
- Неподдерживаемая modality отклоняется стандартной A2A content-type ошибкой до model turn.
- Ни один принятый Part MUST NOT быть отброшен молча. Part, который runtime не может ни передать модели, ни сохранить, MUST завершаться `CONTENT_TYPE_NOT_SUPPORTED`; тихое игнорирование запрещено, потому что caller получает успешный ответ на запрос, часть которого не рассматривалась.

### Файлы в transport

Создавая задачу внешнему агенту, модель выбирает `core_agent_send_message.files`
явно для каждого вызова. Отсутствующее поле или `[]` не прикрепляет файлов;
созданные файлы и `core_response_files` не являются автоматическим выбором.
Transport передаёт сохранённые bytes только выбранного набора через standard raw
Parts; URLs и локальные IDs/пути не дают внешнему агенту доступ к workspace.

UI и A2A принимают вложения вне RunRequest и атомарно сохраняют их в постоянной папке чата по [Файлам и transport artifacts](artifacts.md). Модель получает фактические безопасные имена, размер, media type и путь `/workspace`, а не inline bytes или system instruction. Сообщение только с файлами допустимо: adapter формирует непустой prompt из описания вложений.

Входящий A2A FilePart поддерживает inline bytes; декодирование строгое, лимит считается по декодированным байтам. Ссылочные Parts MAY приниматься только управляемым transport resolver после проверки доступа и фактических сетевых назначений, без передачи credential на произвольный caller URL. Неподдерживаемая ссылка отклоняется до admission, а не игнорируется. Обычный A2A Artifact остаётся формой результата; model-callable artifact tools удалены.

## `mcp`

MCP connection descriptors принадлежат конфигурации развёртывания: Streamable HTTP servers объявляются `MCP_URL`, а allowlist — `MCP_ALLOWED_SERVERS`/`MCP_ALLOWED_TOOLS`. Запрос их не передаёт и не может расширить. Пустая конфигурация означает отсутствие MCP capabilities.

Формат descriptor остаётся прежним; целевой продукт поддерживает stdio и Streamable HTTP:

```json
{
  "name": "repo-tools",
  "required": true,
  "transport": {
    "type": "stdio",
    "command": "repo-mcp",
    "args": ["--root", "."],
    "env_refs": {"TOKEN": "secret/repo-token"}
  }
}
```

```json
{
  "name": "issue-tracker",
  "required": true,
  "transport": {
    "type": "streamable_http",
    "url": "https://mcp.example.test",
    "header_refs": {"Authorization": "secret/issues-auth"}
  }
}
```

Требования:

- `name` уникален внутри развёртывания;
- descriptor MAY запрашивать tools/resources/prompts/sampling/elicitation;
- secrets передаются ссылками на host secret store;
- descriptor проходит allowlist до подключения;
- stdio MCP запускается как owned process в TerminalSession текущего agent-а;
- catalog фиксируется snapshot-ом; notification меняет revision только на safe boundary;
- ошибка `required: true` завершает Task, optional descriptor создаёт наблюдаемый warning.

Optional server объявляется тем же descriptor с `required: false`:

```json
{
  "name": "docs-search",
  "required": false,
  "transport": {
    "type": "streamable_http",
    "url": "https://docs.example.test/mcp"
  }
}
```

Объявление само по себе не делает server доверенным. PlatformConfig/AgentConfig должны подтвердить identity/target. Отфильтрованный конфигурацией descriptor не попадает в effective catalog: `required: true` завершает validation с `CAPABILITY_DISABLED`, optional descriptor создаёт наблюдаемый filtered-capability warning.

## `skills`

Skill package references принадлежат конфигурации развёртывания и задаются `CORE_AGENT_ALLOWED_SKILLS`. Запрос их не передаёт. Пустая конфигурация означает отсутствие пользовательских runtime skills.

```json
{
  "name": "release-notes",
  "source": "skill://company/release-notes@2.1.0",
  "integrity": "sha256-..."
}
```

- `name` уникален внутри развёртывания;
- source resolver разрешён host policy;
- remote immutable source имеет integrity/signature provenance;
- dependency graph фиксируется lock snapshot-ом;
- package проходит [Skills](skills.md) validation;
- A2A Agent Skill в Agent Card и этот runtime skill descriptor являются разными сущностями.

## Совместимость с прежним расширением

Agent Card MUST NOT объявлять `urn:core-agent:run-capabilities:v1` ни required, ни optional. Клиент, продолжающий присылать это расширение, MUST обслуживаться как обычно: и заголовок `A2A-Extensions`, и `Message.extensions`, и payload в metadata игнорируются.

Исключение обязательно: если payload содержит непустые `mcp` или `skills`, запрос MUST отклоняться `CONFIG_INVALID` с указанием, что эти возможности теперь задаются конфигурацией. Молчаливое игнорирование здесь означало бы, что caller получил успешный ответ на задачу, решённую без переданных им серверов и skills.

## Task и background mode

- В enterprise endpoint каждое принятое создание root работы возвращает durable A2A Task, включая сохранённую failed Task при CONTEXT_BUSY.
- Любая Task с tools, background work, memory write или сабагентом MUST вернуть A2A Task.
- `return_immediately` включает non-blocking mode: сервер подтверждает Task и продолжает её в фоне.
- Клиент получает updates через get/list, subscribe/stream или push notifications.
- Закрытие stream не отменяет Task.
- Main agent также использует этот lifecycle для внутренних background/subagent tasks.

## Неполное завершение по budget

Исчерпание execution budget не меняет форму запроса и не вводит новый A2A
terminal state. Runtime публикует обычный text Artifact и завершает Task как
`completed`, но Artifact/result metadata содержит
`completion_reason: "budget_exhausted"`, `complete: false` и исчерпанную
dimension. Сам текст явно отделяет проверенный промежуточный результат от того,
что агент намеревался, но не успел выполнить; отсутствующие tool outcomes не
достраиваются.

Result metadata также содержит локальный `usage`, атомарный snapshot общего
root ledger `shared_budget` и, если cancel не подтвердился за bounded grace,
`pending_tasks`. Live delivery и recovery после crash публикуют эти поля в одной
и той же `provenance` metadata Artifact-а.

Это аддитивное обратно совместимое поле: клиент, не читающий metadata, всё равно
получает самодостаточный текст о неполноте. Отсутствие этих полей в ранее
сохранённом результате означает `completion_reason: "completed"` и
`complete: true`; новая версия публичного binding или миграция JSON rows не
требуются.

## Follow-up в активную Task

Клиент не обязан ждать завершения Task, чтобы написать снова. Follow-up Message указывает существующий `taskId` и тот же `contextId`; A2A adapter сохраняет его в durable inbox текущего run и возвращает ту же Task. `contextId` без `taskId` запрашивает отдельную Task с атомарной проверкой занятости по TASK-01/02 ниже.

Follow-up содержит новый text turn. Он не может изменить EffectiveConfig, tenant, model route, policy или budgets активной Task. Повторный `messageId` идемпотентен; concurrent Messages упорядочиваются server-side sequence.

Runtime добавляет принятый input в model transcript как user-role Message на ближайшей safe boundary. Уже выполняющийся model/tool call не прерывается. Перед завершением Task runtime обязан атомарно проверить, что нет более раннего непрочитанного input. Успешное завершение доставляет такой input модели; при ошибке или отмене runtime durable учитывает его в transcript как необработанный с причиной failure/cancel, не начинает новую работу и только затем фиксирует terminal state. После terminal state продолжение диалога создаёт новую Task в том же `contextId`.

## Результаты

Основной результат Task — A2A Artifact:

- stable artifact ID и revision;
- один или несколько typed Parts;
- media type, size и digest;
- provenance на Task/tool/memory revision;
- `append`/`lastChunk` semantics для streaming, если поддерживаются binding version.

User-facing progress и requests передаются Messages/Task status. Critical result не хранится только в transient status Message.

Финальные файлы передаются ordered standard raw FileParts того же result Artifact,
с безопасными filename/mediaType и точными bytes. `metadata.provenance.outgoingFiles`
содержит только ordered `{file_id,name,media_type,size_bytes,sha256}` receipts.
Raw files не повторяются в terminal Message. Перед первым final frame весь набор
проходит scope, aggregate-limit и integrity проверки; GetTask и recovery сохраняют
те же IDs и содержимое. Отсутствие attachments у прежнего результата совместимо
с пустым набором; wire schema/version самого A2A не меняется.

Owner download `GET /api/chats/{context_id}/tasks/{task_id}/files/{file_id}`
читает immutable final snapshot только после проверки current owner/company,
canonical chat/Task binding и сохранённого final manifest; затем проверяет
size/digest. Известный blob ID, digest или file ID другого чата не является
authority и даёт not-found. Response — attachment, `Cache-Control: no-store`
и `X-Content-Type-Options: nosniff`; private paths/provenance не выдаются.

## Cancellation и passive wait

- Внешняя отмена использует A2A cancel Task operation.
- Внутренний agent может вызвать `core_task_cancel` для child/background Task.
- Passive wait не создаёт внешней terminal state: Task остаётся `working`, status metadata сообщает `waiting_task`.
- Task, ожидающая notification, не удерживает model worker, active terminal process или busy loop.

## Ordering и idempotency

Internal event log имеет monotonic revision/sequence. A2A status/artifact updates содержат достаточную version metadata, чтобы:

- восстановить порядок после reconnect;
- дедуплицировать at-least-once push delivery;
- не применить stale input;
- не перепутать artifact chunks;
- связать внешний Task с внутренним audit.

Inbound Messages используют тот же принцип: `(task_id, message_id)` уникален, а committed inbox sequence задаёт порядок model delivery. Inbox append и terminal transition сериализуются так, чтобы сервер не подтвердил Message, которое Task затем потеряет.

## Terminal semantics

Каждая Task достигает ровно одного A2A terminal state: `completed`, `failed`, `canceled` или `rejected`. Внутренний `ABORTED` отображается в `failed` с безопасным `unsafe_continuation` reason.

После terminal state новые Messages этой Task отклоняются стандартной A2A terminal-task ошибкой. Продолжение диалога создаёт новую Task в том же `contextId`.

## Embedded SDK

Embedded/local SDK MAY предоставить convenience `run(prompt)` и typed event iterator. Он MUST:

- использовать ту же A2A Task/message/artifact semantics;
- отдавать Agent Card/capability metadata;
- не создавать другой lifecycle;
- позволять поднять A2A binding без изменения domain behavior.

## Совместимость

- Добавление optional A2A status metadata обратно совместимо.
- Persisted Task хранит обе versions для replay/migration.
- Live steering использует стандартные `Message.taskId/contextId/messageId` и не добавляет полей к контракту.

## Enterprise endpoints и приём сообщений

### API-01. Разделение входов

- A2A endpoint `/a2a/owner` для владельцев.
- A2A endpoint `/a2a/external` для внешних агентов.
- Owner API под `/api`, включая отдельный endpoint решений HITL `/api/hitl`, доступен только владельцам. Наличие маршрута в target contract не означает готовый UI/HITL flow.
- Оба A2A endpoint используют один runtime и Task lifecycle. Public probes `/health/live` и `/health/ready` остаются без аутентификации; они не раскрывают пользовательские данные.
- Tenant/identity/права выводятся из проверенного transport context, а не из
  текста сообщения, поля metadata или аргументов инструмента.

Production требует полную Keycloak configuration и закрывает прежние обходные
пути. Только explicit development/test с полностью ненастроенной auth может
сохранять legacy routes; частично настроенная или недоступная auth никогда не
включает fallback. Входящий Authorization не передаётся внешнему агенту.

### TASK-01. Одна активная корневая задача

В одном чате одновременно выполняется не более одной корневой задачи.
Ожидание HITL, ответа владельца, внешнего агента или времени пробуждения
сохраняет занятость чата. Разные чаты могут выполняться независимо.
Внутренние дочерние задачи и делегирование принадлежат корневой задаче;
это ограничение не запрещает их разрешённый параллелизм.

### TASK-02. Новая задача при занятом чате

Сообщение в существующий `contextId` без `taskId` трактуется нашим контрактом
как запрос новой задачи. Если чат занят:

- сохраняется отдельная запись новой A2A Task с собственным `taskId`;
- она сразу имеет терминальное состояние `failed` (`TASK_STATE_FAILED` в A2A 1.0);
- причина — наш машинный признак `CONTEXT_BUSY`, понятное пояснение и
  `activeTaskId` текущей доступной вызывающему задачи;
- выполнение новой задачи не начинается, очередь не создаётся;
- текущая задача продолжает работу;
- ответ является объектом неуспешной Task, а не одновременно JSON-RPC error;
- GetTask возвращает тот же сохранённый результат попытки.

Проверка занятости и регистрация успешного старта должны быть атомарными:
два одновременных запроса не могут запустить две корневые задачи одного чата.
Завершившаяся запись `failed` сама не занимает чат.

Повтор запроса создания с тем же `messageId` и тем же содержимым возвращает
ту же Task с её актуальным состоянием, без повторного запуска или публикации
копий вложений. Это правило действует и после завершения Task, включая
сохранённый отказ CONTEXT_BUSY: новая попытка запуска требует нового messageId.
Если при прежнем messageId содержимое изменилось, возвращается ошибка конфликта,
исходная задача не изменяется.

Дедупликация scoped по tenant и стабильной authenticated identity вызывающего;
совпадение messageId у другого caller-а не раскрывает чужую Task. Каждое
обращение предварительно проверяет текущие права. При сравнении учитываются
содержимое сообщения, context и вложения, а не значение access token.
Связь messageId с Task сохраняется атомарно при приёме, переживает restart
и замену токена. Проверка повтора предшествует созданию новой failed Task
из-за занятости чата; конкурентные повторы не создают несколько задач.

В wire representation busy Task содержит
`metadata.error = {"code": "CONTEXT_BUSY", "activeTaskId": "..."}` и
пояснение в `status.message`. Changed-message конфликт передаётся стандартным
A2A InvalidParamsError с `data.code = "MESSAGE_ID_CONFLICT"`; отдельная Task
при конфликте не создаётся. Пустой messageId и исходный role, отличный от
ROLE_USER, отклоняются до admission.

Fingerprint version 1 — SHA-256 canonical JSON исходного A2A Message до
назначения сервером отсутствующих IDs. Protobuf MessageToDict преобразуется
через JSON с отсортированными ключами, ASCII escapes и разделителями `,`/`:`
без пробелов, затем кодируется UTF-8. В сравнении участвует весь Message,
включая parts, metadata, extensions, references и переданный context;
HTTP credentials, trace headers и response configuration не участвуют.
Отсутствие contextId отличается от явно переданного contextId: повтор первого
запроса не должен подменять его серверным ID. Изменение алгоритма требует новой
fingerprint version и чтения старого ledger по его сохранённой версии.

### TASK-03. Сообщение в активную задачу

- Сообщение с `taskId` существующей нетерминальной задачи является follow-up.
- Оно сохраняется durable при приёме; текущий tool call не прерывается.
- После результата текущего инструмента сообщение добавляется в контекст
  перед следующим обращением к модели.
- Если инструмент сейчас не выполняется, доставка происходит на ближайшей
  безопасной границе agent loop.
- Порядок сообщений сохраняется; повтор одного `messageId` в задаче не создаёт
  повторного user turn.
- Принятое сообщение не теряется при гонке с завершением и при перезапуске.
- Follow-up не является решением HITL и не повышает права вызывающего.
- Для терминальной задачи требуется новый Task в том же чате.

Если задача ожидает HITL или ответ внешнего агента, follow-up сохраняется
сразу, а модели передаётся после завершения ожидания, перед следующим
обращением к ней. Уточнение само не завершает ожидание, не заменяет решение
HITL и не продлевает deadline. Это правило согласовано отдельно.
При ожидании заданного времени по LONG-03 принятое уточнение, наоборот,
прерывает ожидание и сразу возобновляет агента для обработки сообщения.
Порядок при пакете нескольких tool calls должен сохранять корректные пары
вызов/результат для model API; adapter MUST сохранить эту гарантию.


## Owner API: настройки и решения

`GET /ui/config` — отдельный публичный bootstrap для браузерного входа, доступный
только при настроенном `KEYCLOAK_UI_CLIENT_ID`. Ответ содержит ровно `{issuer,
client_id}` из deployment configuration. Confidential introspection client и его
secret, audience, company, токены и пользовательские сведения не выдаются.
Query parameters не допускаются (`400 REQUEST_INVALID`); client/issuer нельзя
переопределить запросом. При выключенном bootstrap маршрут даёт 404.
Ответы имеют `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` и CSP `default-src 'none'`. Public bootstrap не
ослабляет авторизацию `/api/`, `/a2a/` и любых file/material routes и не делает
произвольный `/ui/` path публичным.

При настроенном browser client и наличии собранного UI сервис публично отдаёт
только `GET/HEAD /ui/`, `/ui/index.html` и перечисленные build assets в
`/ui/assets/`; `/ui` перенаправляет на `/ui/`. Unknown asset/path даёт404,
произвольные файлы приложения и symlinks не выдаются. Отсутствующий build или
выключенный browser client не включает эти routes. SPA shell не перехватывает
`/api/`, `/a2a/` и их ошибки авторизации/404. OAuth callback параметры shell
не выбирают issuer/client/scope и не влияют на отдаваемый build.
Shell имеет no-store, assets — immutable cache только при build filename;
все ответы имеют nosniff и no-referrer. CSP запрещает чужие scripts/styles,
embedding, object и base overrides; connect-src включает только self и origin
настроенного Keycloak issuer. Browser bundle не содержит secrets или
build-time credentials. Public assets не снимают server auth с private API.

Public browser client разрешает exact login URI `/ui/` и exact logout URI
`/ui/?logged_out=1` на origin агента; допустим существующий scoped wildcard
`/ui/*`. Перед обновлением вручную настроенного exact-only клиента добавляется
logout URI в allowlist Keycloak; application schema и persisted Tasks не меняются.

Кнопка выхода завершает текущую SSO-сессию Keycloak, очищает токены и приватное
состояние UI в памяти и возвращает на `/ui/?logged_out=1` с сообщением
«Вы вышли из аккаунта» и кнопкой «Войти». Экран выхода, в том числе после
перезагрузки, не запускает автоматический вход или private API requests.
Вход начинается только по нажатию кнопки и использует прежний PKCE flow.
Очистка токена при logout, expiry или ошибке доступа не запускает вход
самостоятельно. Маркер выхода не содержит credentials, не меняет права
и не отменяет уже принятые Tasks. Новый запрос со старым токеном завершённой
Keycloak-сессии отклоняется обычной проверкой авторизации.

В чате остаются видимыми активные запросы разрешений на tools и проверки
guardrails. После canonical сохранённого решения, включая отказ и timeout,
их карточки скрываются и не возвращаются после обновления истории или reload.
Локального достижения deadline недостаточно для скрытия до сохранения outcome
сервером. Решения и audit сохраняются с прежними правами доступа; ответы
владельцев на вопросы агента остаются видимыми. Разрешение не является
доказательством выполнения действия; tool results отображаются отдельно.

Ответы агента, включая сохранённую историю и прямой ответ A2A, отображаются
как CommonMark с GFM-таблицами, списками и блоками кода. Пользовательский ввод
и raw tool records сохраняют текстовое отображение. HTML из Markdown не
исполняется, опасные URL не становятся ссылками, изображения из Markdown не
загружаются автоматически. Блоки `mermaid` отображаются как статические
SVG-изображения в image context без scripts, интерактивных действий и доступа
к странице/credentials. CSP допускает `blob:` только в `img-src`; scripts,
styles, connections и frames не получают дополнительных разрешений.
Некорректная или превышающая лимит диаграмма сохраняет исходный код и видимое
сообщение об ошибке вместо исчезновения ответа. Настройки диаграммы из
недоверенного текста не ослабляют securityLevel или лимиты рендерера.
Обновление истории или статусов при неизменном тексте сохраняет уже
отрендеренную диаграмму и не переключает её обратно на исходный код.

### Представление owner UI

UI не показывает название продукта, логотип или повторяющиеся подписи автора
в навигации, над ответами и на экранах входа/выхода. Заголовок содержит название
текущего чата; title вкладки — «Чат». Содержимое сообщений
владельца/модели и canonical technical tool names сохраняются как данные.

Основное содержимое чата — запрос владельца и ответ агента. Вызов инструмента
и его результат составляют одну карточку по `(task_id, tool_call_id)`, которая
обновляется без дублирования. Заголовок формируется из фактического инструмента
и аргументов; успешное выполнение, ошибка, ожидание и неопределённый исход
различаются по сохранённым данным, а не по словам модели. Доступный после
проверок stdout/stderr показывается обычным текстом с переносами строк по раскрытию.
Для failed process built-ins history допускает только валидированные скалярные
метаданные ExecutionResult: exit code, duration, timed_out, truncated и status.
Тексты исключений и ошибок провайдера/MCP остаются скрытыми; guardrails withholding
имеет приоритет и над этими метаданными. Имя инструмента, аргументы, exit code
и ID доступны отдельно в технических данных. Последовательные
успешные действия можно сворачивать в «Ход выполнения»; раскрытые успешные
действия имеют ограниченный по высоте, доступный с клавиатуры scroll region,
чтобы итоговый ответ оставался доступным. Предпросмотр ограничивает свой
контент внутри отдельного scroll region и не перекрывает свойства/следующие
сообщения. Ошибки и активные
разрешения всегда видимы. Отдельного технического режима нет.

Название диалога отделено от состояния текущего запроса и состояния соединения.
Статус использует canonical Task, активные waits и tool outcomes; «разрешено»
не означает «выполнено». Во время работы доступна остановка, при ожидании
владельца — переход к запросу; ожидание внешнего агента или времени поясняется.
Ошибки и неопределённый результат имеют видимую причину и безопасное действие.
Статусы объявляются через polite live region без перемещения фокуса.
Автоматическое обновление истории является основным сценарием; ручное обновление
находится в шапке. Успешная синхронизация не занимает постоянное место в чате.
Первичное подключение и нормальное завершение SSE для terminal Task не считаются
потерей соединения. При недоступной подписке и успешном canonical GetTask UI
продолжает polling без ложного сообщения о потере связи. Ошибка canonical чтения
показывает восстановление до следующего успешного чтения; auth/access errors
сохраняют прежние правила завершения сессии. Pending guardrail placeholder имеет
подпись «Обработка поручения»; фактические кнопки owner review остаются видимыми.

Активная карточка разрешения показывает конкретное действие и существенные
параметры до кнопок: команду, адресата/сообщение/вложения, объект изменения или
расписание. Эти сведения берутся из сохранённого subject, credentials не
материализуются. Deadline подписан «Ответить до» с часовым поясом. Подтверждение
разрешает только этот вызов; владелец может разрешить либо отклонить. Завершённые
tool approval/guardrail карточки остаются скрыты согласно ENT-AC-73.

Экран инструментов — компактный список с человекочитаемыми названиями,
техническими именами для поиска/деталей, поиском, фильтром источника и режима,
быстрым фильтром отключённых проверок. «Режим выполнения» содержит
«Без подтверждения» / «С подтверждением» / «Запрещено».
Положительный переключатель «Проверять аргументы и результаты» равен
`!guardrails_exempt`; изменение подписи не меняет существующие правила.
Настройки общие для этого агента и всех его чатов. Сохранение явное для каждого
изменённого правила с прежними revision/origin checks; пакетного сохранения нет.

Вложения показываются карточками с именем, типом, понятным размером и действиями
«Открыть»/«Скачать». Системное описание вложений и `/workspace` пути для модели
не выдаются за текст владельца: UI использует canonical original display text
при наличии provenance. Legacy записи без доказуемого original text не очищаются
эвристическим regex. Путь остаётся в раскрываемых свойствах. Предпросмотр
недоверенного текста экранируется; HTML/SVG/Office не исполняются как страница
origin агента. Markdown-файлы используют тот же безопасный Markdown/Mermaid renderer,
что и ответы. Python code blocks, previews и фактические параметры Python
получают подсветку синтаксиса без исполнения кода, raw HTML или изменения
отступов. Крупный код отображается простым текстом с тем же сохранением
отступов. Неподдерживаемый формат предлагает скачивание. Все file requests
проходят прежнюю owner/scoped авторизацию; смена чата/logout отменяют загрузку.
Карточки созданных файлов находятся у результата. Панель «Файлы» показывает
«Прикреплённые»/«Созданные агентом», количество и переход к исходному сообщению,
а существующая фильтрация/ручная очистка workspace остаётся доступной.

Чаты имеют содержательные названия по первому доступному запросу или вложению
и сохранённое владельцем переименование. UUID находится в меню «Скопировать ID».
В списке видны только название и состояния, требующие внимания; дата и время
обновления не отображаются. Порядок по последнему обновлению сохраняется.
Шапка показывает название текущего чата. Переименование
owner-only, company-scoped, ограничено 120 символами и использует CAS;
оно не меняет task/context IDs, историю, права, workspace или instructions.

Поле ввода начинается с двух строк, растёт до ограниченной высоты и прокручивается
далее. Лимит обозначает сумму вложений одного сообщения в понятных единицах.
Обновления не прокручивают читающего историю пользователя вниз; появляется
«Новые сообщения ↓». История имеет запас места под composer; sticky controls
не закрывают фокус. Монохромное оформление сохраняется, enabled/disabled/
danger/focus состояния различимы; обычный текст имеет контраст не менее 4.5:1,
крупный — 3:1, необходимые границы и focus indicators — 3:1.

Каждый маршрут ниже требует verified owner Principal той же company. Внешний
caller получает 403 до поиска идентификатора; owner другой company — 404.
Владелец принимает решение по external-owned Task без изменения её owner_id.
Tenant, owner, actor и права не принимаются из JSON. Неизвестные поля JSON
отклоняются; ответы содержат `Cache-Control: no-store`.

| Method/path | Контракт |
| --- | --- |
| `GET /api/chats` | Общий список canonical чатов company: `context_id`, `latest_task_id`, `active`, `status` последнего root workflow, owner-only `needs_attention` текущего unresolved/unexpired owner wait, `title`, `title_revision` и `updated_at` (Unix seconds). Автоматическое название раскрывается только при доступности исходного сообщения/вложения; withheld material не раскрывается через metadata. Название владельца хранится независимо. Только владельцы; без caller credentials и private material. Limit 1–100 (default 50), стабильный порядок по context_id, непрозрачный cursor, привязанный к company; ответ `{chats, next_cursor}`. Чтение не создаёт чат и не запускает/возобновляет задачу |
| `PUT /api/chats/{context_id}/title` | Owner-only переименование: ровно `{title, expected_revision}`; trim, непустое название до 120 Unicode символов, неотрицательная integer revision. Ответ `{context_id, title, title_revision, updated_at}`. Неверное тело —400 `REQUEST_INVALID`, роль —403, missing/foreign chat —404 `TASK_NOT_FOUND`, stale revision —409 `CHAT_TITLE_CONFLICT`. Смена названия не меняет Task/context/workspace или prompt; все владельцы company видят сохранённое значение |
| `DELETE /api/chats/{context_id}` | Owner-only удаление из общего списка: без body/query; ответ200 `{context_id, archived:true}` также при повторе. Missing/foreign404 `TASK_NOT_FOUND`, external/dual role403, любой canonical nonterminal root409 `CONTEXT_BUSY`. Архивирование и отключение всех связанных enabled cron выполняются атомарно. Старые Tasks, история, workspace и выданные transport files сохраняются |
| `GET /api/chats/{context_id}/history` | Owner-only полная история canonical чата, включая предыдущие root Tasks и принятые уточнения во время ожидания. Limit 1–100 (default 50), newest-first, versioned cursor, привязанный к company/chat и сохранённой позиции; ответ `{items, next_cursor}`. Missing/foreign chat —404; неверные query/cursor —400; чтение не запускает runtime |
| `GET /api/chats/{context_id}/files` | Owner-only preview обычных файлов постоянного workspace. Optional `directory` — относительный каталог (default корень), `older_than_days` — неотрицательное конечное десятичное число до 100000, `limit` 1–100 (default 50), `cursor`; неизвестные/повторные query запрещены. Ответ `{files, next_cursor, listed_at, active, cleanup_block_reason, workspace_revision}`; элемент содержит ровно `name`, `path`, `size`, `mtime_ns`, `identity_token`. Порядок lexicographic по относительному пути, cursor связан с company/chat/каталогом/фильтром, revision и server timestamp; чтение не создаёт папку и не запускает runtime |
| `GET /api/chats/{context_id}/files/content?path=...` | Owner-only скачивание существующего обычного файла выбранного workspace; ровно один непустой относительный POSIX path. Свежая проверка company/chat и безопасное открытие по directory fds; symlinks, hardlinks и special files не выдаются. Attachment, `application/octet-stream`, no-store, nosniff и sandbox CSP. Missing/foreign file —404, malformed path/query —400; descriptor закрывается и при disconnect |
| `POST /api/chats/{context_id}/files/delete` | Owner-only удаление явно выбранного набора: ровно `{request_id, files:[{path, identity_token}]}`. Не более 1000 уникальных относительных paths; request_id — непустая UTF-8 строка до 256 байт без NUL. Тот же company/chat/request_id и неизменное упорядоченное тело возвращают прежнюю операцию; другое тело —409 `CLEANUP_REQUEST_CONFLICT`. Empty selection не меняет файлы. Active Task —409 `CONTEXT_BUSY`, без создания/очереди операции. Completed receipt —200, pending/reconciliation —202 |
| `GET /api/chats/{context_id}/files/delete` | Owner-only read-only receipt: optional единственный `request_id`, без него —последняя сохранённая операция чата. Все владельцы company могут прочитать receipt и явно повторить прежнее подтверждённое тело. Missing/foreign receipt —404 `FILE_CLEANUP_NOT_FOUND`. GET не запускает очистку и не возобновляет операцию |
| `GET /api/interactions?task_id=...&status=pending` | Запросы указанной Task и её root family в том же чате; status `pending` или `all`, limit 1–100 (default 50), непрозрачный cursor, привязанный к task_id и status; immutable subject, digest, generation, deadline и сохранённый outcome доступны владельцам. Timer и local-task waits в этот список не входят |
| `POST /api/hitl/{wait_id}/decision` | Ровно `decision: allow|reject` и `subject_digest`; аргументы вызова изменить нельзя |
| `POST /api/questions/{wait_id}/answer` | Ровно непустой `answer` до 64 KiB UTF-8 и `subject_digest` |
| `POST /api/guardrails/{wait_id}/decision` | Ровно `decision: allow|reject` и `subject_digest`; kind ожидания обязан быть guardrail |
| `GET /api/guardrails/{wait_id}/material` | Owner-only чтение сохранённого материала указанного guardrail wait: public interaction metadata и private payload либо sealed file reference. Tenant берётся из principal, run/owner — из сохранённого ожидания; query/body не меняют scope. Чужой или неподходящий wait имеет not-found semantics; ответ `Cache-Control: no-store` |
| `GET /api/guardrails/{wait_id}/file` | Owner-only скачивание конкретного файла по сохранённому sealed reference проверки; batch/index/run/owner не принимаются от клиента. Scope и bytes/digest проверяются также после завершения Task. Ответ: attachment, `application/octet-stream`, no-store, nosniff и sandbox CSP; неподходящий/чужой review — 404, повреждённые bytes не выдаются |
| `GET /api/tool-policies` | Известный catalog внутри deployment ceiling с origin, mode, guardrails_exempt и revision |
| `PUT /api/tool-policies/{canonical_name}` | Ровно `mode: allow|require_hitl|deny`, boolean `guardrails_exempt`, integer `expected_revision`, string `expected_origin` из прочитанного catalog |

Удаление чата является необратимой меткой архивации, а не удалением сохранённых
A2A-результатов. UI требует предметного подтверждения, закрывает удалённый чат
и убирает его из общего списка всех владельцев company. Новый root/follow-up,
переименование, создание/включение расписания и новый manual/automatic cron
run для архивного context запрещены без переиспользования его workspace.
Уже сохранённые dedup receipts возвращают прежний результат без новых эффектов.
GetTask/Subscribe и прежние owner history/file reads сохраняют прежнюю scoped
авторизацию и доступ к результатам. Migration26 добавляет monotonic tombstone
без изменения identity, history, immutable files или accepted-message ledger.
Архивация сериализуется с admission и cron по chat→schedule lock order;
workflow-fenced agent tool writers не инвертируют этот порядок.

Chat metadata и `display_text` являются additive полями owner API. Migration 25
сохраняет существующие Task/context/workspace identities и request dedup ledger;
старый nullable original text не заполняется разбором служебного suffix. Legacy
название может выводиться только из доказанного доступного оригинала. Новые
clients принимают отсутствие optional metadata при чтении старого контракта;
serving image и DB schema обновляются согласованно отдельной migration job.

Workspace preview использует original immutable chat owner из canonical mapping,
а не actor ID просматривающего владельца. `listed_at` — Unix seconds server clock;
cursor сохраняет исходное время списка с точностью nanoseconds. Возраст файла
сравнивается строго с N суток по 86400 секунд, поэтому точная граница исключена.
`active` выводится из актуального canonical root, включая все durable waits;
`cleanup_block_reason` равен `CONTEXT_BUSY`, `WORKSPACE_CLEANUP_PENDING` либо null.
`workspace_revision` — неотрицательная durable revision чата. Preview доступен при active
Task и не удаляет файлы. Известный чат без созданной папки даёт пустой список;
неизвестный/чужой чат или отсутствующий явно выбранный каталог имеет 404.

Preview cursor version 2 подписан process-local key и связан с company/chat owner,
directory, фильтром, revision, временем и последним путём. Подмена, устаревшая
revision, прежний cursor v1 либо restart дают 400;
владелец обновляет preview. Cursor не является bearer authority; каждый запрос
повторно проверяет роль и scope. Пагинация не обещает snapshot изменяющейся файловой
системы. Identity token version 2 учитывает scope/path, durable workspace revision
и device/inode/ctime/mtime/size. Старый token v1 либо изменившийся token считается
stale selection и пропускается; сервер не заменяет его свежим token автоматически.

Traversal использует nofollow directory fds; обычные файлы с единственной ссылкой
перечисляются, symlinks/hardlinks/devices/FIFO и control manifests опубликованных
attachments не выдаются. Private staging/quarantine находятся вне workspace.
Один preview ограничен 10000 просмотренных entries и глубиной 64; превышение
возвращает `WORKSPACE_SCAN_LIMIT`/409 вместо неполного успешного списка. Владелец
может выбрать более узкий directory. Ошибка доступа/гонка безопасного открытия
возвращает `WORKSPACE_UNAVAILABLE`/409, не открывая соседний путь. File download
держит проверенный descriptor до окончания ответа, читает bounded chunks не
дальше размера на момент открытия и не обещает immutable snapshot live-файла.

Cleanup receipt содержит ровно `request_id`, `operation_id`, `state`,
`workspace_revision`, `files`, `results`, `totals`. `files` — исходные подтверждённые
`{path,identity_token}` без filesystem/private proofs. `state` —
`pending|completed|reconciliation`; для каждого submitted path `results` содержит
`path`, `status: pending|deleted|skipped|error`, optional `reason`/`size`.
Reasons — `missing|identity_changed|unsafe_file|protected_file|filesystem_error|
reconciliation_required`. `totals` содержит `deleted`, `skipped`, `errors`,
`deleted_bytes`; pending не считается удалённым или ошибкой. UI показывает
частичные результаты, а не общий успех. Повтор uncertain network сохраняет
request_id и точное тело; автоматического POST retry либо новой selection нет.

Очистка и admission используют один canonical chat lock. Immutable intent с
original owner, selection, stat/parent proofs, content digest и base revision
коммитится до filesystem mutation; затем исполняется или восстанавливается под
тем же lock. Пока принятый intent не завершён, новый root получает retryable
`WORKSPACE_CLEANUP_PENDING` до admission, без Task/модели/файловых effects;
существующий duplicate Task receipt остаётся прежним. Это не очередь попыток
очистки при busy Task. Неверный persisted protocol даёт
`WORKSPACE_CLEANUP_INVALID`/409 и сохраняет barrier, неизвестная версия не исполняется.
HTTP+JSON send/stream возвращает 503 `UNAVAILABLE` до открытия stream;
JSON-RPC возвращает server error -32000 с исходным request ID. Typed
`google.rpc.ErrorInfo` содержит reason `WORKSPACE_CLEANUP_PENDING` и только safe
metadata `{code:"WORKSPACE_CLEANUP_PENDING", retryable:"true"}`.

Файл перед unlink атомарно захватывается без замены в private staging вне
workspace; исходный descriptor/stat/digest и parent-chain proofs проверяются.
Content digest снимается при intent с pre/post stat, затем проверяется после
capture, поскольку rename меняет ctime и один stat недостаточен для обнаружения
изменённых bytes с прежними size/mtime. Journal и оба каталога fsync-ятся до
терминального receipt; private deletion proof позволяет восстановить исход
после unlink и DB rollback. Отсутствие исходного пути само по себе не доказывает
удаление. Подменённый captured object восстанавливается без перезаписи; конфликт
сохраняет bytes и reconciliation/barrier. Recovery не делает blind unlink по
исходному имени. Допускается только продолжение прежнего подтверждённого intent;
все владельцы могут явно проверить/повторить его. Revision увеличивается один
раз при завершении операции с verified deletion; stale preview требует обновления.

History item содержит ровно `id`, `task_id`, `kind`, `text`, `status` и при
необходимости `review`/`outcome`/`attachments`. `id` — стабильная server-owned identity,
`task_id` — root Task, `kind` — `user_message|agent_message|tool_call|tool_result|
result|placeholder`. `status` — `available|queued|pending_guardrail|rejected|
timed_out|unprocessed_due_to_failure|unprocessed_due_to_cancel`. `review` содержит
ровно `wait_id` для существующего owner review API; ссылки и sealed refs из
непроверенного материала не используются. `outcome` у final result содержит
`state` и при наличии сохранённые `complete`, `completion_reason`, `error_code`.
Error — safe code, без exception/provider text. Tool calls/results проецируются
явно, без provider replay, hidden reasoning, auth/trace/private snapshot fields.
Текст отображается как данные и не разрешает использование материала моделью.

После публикации входящего file batch доступная запись сообщения MAY включать
`attachments`: ordered array объектов ровно с `index`, `actual_name`,
`relative_path`, `size_bytes`, `sha256` из сохранённого server-owned receipt.
History заново проверяет связь batch с company, chat owner, context, Task, run
и input sequence. Исходные имена, media metadata и bytes не возвращаются этим
полем. Для queued, pending, excluded или rejected материала имена не раскрываются
через history; просмотр карантина остаётся отдельным owner review API.

Task metadata исходного принятого сообщения с файлами содержит `file_batch_id`
и `file_receipt` с ровно `schema_version`, `batch_id`, `created_at`, `source`,
`total_bytes`, `entries`.
Успешный ACK follow-up MAY дополнительно включать `accepted_message_id` и
`accepted_file_receipt` в response copy Task. ID должен совпадать с отправленным
messageId; receipt берётся из canonical inbox provenance, включая dedup. Этот
response не заменяет immutable root receipt или persisted Task history.

Отказ file admission использует safe SDK ErrorInfo: `metadata.code` и при
превышении `allowed_bytes`/`actual_bytes` как decimal strings, без file bytes,
исходных имён или metadata. HTTP+JSON возвращает INVALID_ARGUMENT400,
JSON-RPC — -32602 с массивом ErrorInfo в `error.data`. До SDK malformed JSON,
повторные keys, invalid UTF-8/nesting дают REQUEST_INVALID400/-32700; compressed
body — CONTENT_TYPE_NOT_SUPPORTED400/-32005. Независимый encoded body ceiling
даёт REQUEST_TOO_LARGE413/RESOURCE_EXHAUSTED (JSON-RPC -32602); корректный
base64 проверяется до SDK, company decoded aggregate — при canonical admission.

История читается из полного local transcript, retained inbound rows и canonical
previous-root chain; active summary и импортированные сообщения не копируются
повторно. Каждый final result публикуется один раз, включая partial/failed.
Принятое unread уточнение немедленно имеет собственную entry со статусом queued;
доставка/отказ сохраняют её identity вместо второго сообщения. Пока материал не
проверен или отклонён, inline payload заменяется понятным placeholder; при наличии
review владелец открывает полный материал через отдельный authorized API.
Внутренние вопросы/ответы доступны владельцам, external/dual-role не получает
history route даже для своей Task. Owner чужой company не получает её данные.

Доступная final entry завершённой Task содержит optional `response_files` —
ordered `{file_id,name,media_type,size_bytes,sha256}` receipts из её immutable
manifest. Private blob IDs, paths, scope, raw bytes и лимит в history не выдаются.
Чтение истории проверяет manifest и canonical binding без загрузки blob content;
скачивание по route final files заново проверяет scope и integrity всего набора.
UI показывает имена/размеры и выполняет отдельный authenticated download только
по явному нажатию владельца. Прежние entries без поля означают пустой набор.

Cursor закрепляет последнюю выданную root/позицию; новые roots, append и compaction
не меняют уже прочитанные позиции. Для новых сообщений UI перечитывает первую
страницу и объединяет entries по стабильному ID. Cursor не содержит произвольного
snapshot и не открывает чужой/child/legacy-unmapped run. В каждом запросе читается
ограниченная страница под canonical company/chat scope, без writer lease,
classifier/model/tool dispatch и изменения wait/deadline/budget. Private responses
имеют no-store. Старые snapshots без history provenance читаются по совместимому
детерминированному порядку из CONTEXT-03, без восстановления неизвестного времени.
| `GET /api/settings` | Три исходных timeout, `attachment_limit_bytes`, remote timeout/poll interval и текущая revision |
| `PUT /api/settings` | Все три исходных timeout и integer `expected_revision`; optional integer `attachment_limit_bytes`, `remote_timeout_seconds`, `remote_poll_interval_seconds` |
| `GET /api/remote-agents` | Company registry metadata, без header values/ciphertext. Limit1–100 (default50), стабильный порядок по server ID, company-bound cursor; ответ `{agents, next_cursor}`. Чтение не вызывает discovery или модель |
| `POST /api/remote-agents` | Создаёт адресата с immutable company-unique `name`; обязательны `name`, `url`, `description`, `enabled`, `header_name`; optional `header_value` (строка либо null). Server назначает `id`, revision1. Повтор name —409 `REMOTE_AGENT_CONFLICT` |
| `PUT /api/remote-agents/{id}` | Полная замена `url`, `description`, `enabled`, `header_name` при integer `expected_revision`; optional `header_value`: omission сохраняет секрет, null очищает. Name/id не меняются. CAS создаёт новую immutable revision; несовпадение —409 |
| `DELETE /api/remote-agents/{id}` | Ровно integer `expected_revision`; создаёт новую disabled revision и сохраняет старые revisions/credentials для принятых операций. Ответ —metadata новой revision; повтор со stale revision даёт409 |
| `DELETE /api/remote-agents/{id}/connection` | Ровно integer `expected_revision`; атомарно создаёт disabled revision без нового credential и необратимо удаляет регистрацию из общего списка и новых обращений. Старые revisions/credentials сохраняются для принятых операций. Ответ —metadata disabled revision; stale active revision даёт409, уже удалённый/чужой ID —404. Имя можно использовать для нового подключения с новым ID |

Timeout keys: `hitl_timeout_seconds`, `owner_answer_timeout_seconds`,
`guardrails_timeout_seconds`. Default каждого — 86 400 секунд; допустимы целые
1–2 147 483 647, boolean числом не считается. Изменение влияет только на новые
ожидания. Revision начинается с 0; конфликт compare-and-set возвращает 409
`SETTINGS_CONFLICT`. Неизвестный инструмент не создаётся owner request-ом.

`attachment_limit_bytes` по умолчанию равен 25 000 000; допустимы целые
1–2 147 483 647, boolean не допускается. Это одна company setting для суммы
декодированных входящих/исходящих вложений сообщения. Её чтение и изменение
подчиняются той же owner role и revision. Для совместимости PUT без нового поля
изменяет только таймауты, сохраняя текущий лимит. Изменение не пересматривает
уже принятые manifest и не запрещает владельцу читать сохранённые файлы.

Remote settings принадлежат той же settings revision: `remote_timeout_seconds`
по умолчанию86 400, `remote_poll_interval_seconds` —300. Допустимы целые
1–2 147 483 647, boolean запрещён. Старый PUT без этих полей сохраняет их
значения. Изменения задают параметры новых операций, не меняя принятый deadline
или poll interval. GET settings включает оба поля.

Registry metadata содержит ровно `id`, `name`, `url`, `description`, `enabled`,
`header_name`, `has_header_value`, `revision`. Все registry routes требуют owner
и no-store; external/dual-role403 до lookup, чужой company ID —404
`REMOTE_AGENT_NOT_FOUND`. Секретные значения не возвращаются после записи,
включая errors, cursor, audit, модель и process environment.
UI предлагает «Удалить» с подтверждением имени подключения и последствий:
новые обращения запрещены, уже принятые операции продолжаются по закреплённым
revisions. Удаление не отзывает входящий Keycloak доступ внешнего агента,
не отменяет Task и не удаляет историю или файлы. Чтение current revision,
изменение и отключение удалённого ID возвращают404; внутреннее чтение exact
historical revision остаётся доступным под прежним company scope. Пагинация
принимает прежний cursor после удаления его anchor. Отключённые подключения
остаются видимыми; прежний DELETE без `/connection` сохраняет эту семантику.
После неизвестного HTTP исхода UI перечитывает список без автоматического
повтора DELETE; позднее чтение не восстанавливает удалённую строку.
Граница принятия исходящей операции — durable `remote.pinned`. После сетевого
discovery текущие enabled/revision/регистрация проверяются под registry lock,
а проверка и запись pin выполняются в одной PostgreSQL transaction. Если
удаление завершилось раньше pin, запоздавший discovery не разрешает обращение.
Уже закреплённая операция использует exact revision без этой current-проверки.
Запоздавшее сохранение закрытого редактора не закрывает новый редактор и не
сбрасывает введённые в нём данные.
POST/PUT/DELETE registry routes не принимают query parameters. Неверный JSON или
набор полей запроса возвращает400 `REQUEST_INVALID` до записи.
Decoded peer ID с NUL или некорректным Unicode отклоняется до lookup с400
`REMOTE_AGENT_INVALID`; неизвестный корректный ID сохраняет404 semantics.

Name —1–128 ASCII `[A-Za-z0-9_-]`; URL —до4096 UTF-8 bytes, description —до4096
UTF-8 bytes (может быть пустой; NUL запрещён), header name —HTTP token до128 ASCII characters.
UI объясняет ограничения имени рядом с полем и отклоняет неверное имя до
отправки запроса; допустимый дефис проходит browser validation.
URL использует HTTPS; HTTP разрешён только для loopback trusted targets.
Userinfo, query, fragment, whitespace/control characters и некорректный port
запрещены. Header name не может переопределить Host, Content-Type,
Content-Length, Connection, Transfer-Encoding, Upgrade, Trailer, TE,
Proxy-Authorization, Accept или A2A-Version. Header value —непустая строка
до16 384 bytes с безопасной HTTP encoding и без control characters; null или
omission при создании означают отсутствие credential. UI использует
`Authorization` как исходное имя и полное значение `Bearer …`; приложение
не добавляет credential prefix автоматически. Неверные значения fields дают400
`REMOTE_AGENT_INVALID`. Credential data шифруются с binding company/peer/
revision/header name; без подходящего encryption key операции с секретами
завершаются `REMOTE_AGENT_CREDENTIAL_UNAVAILABLE`, без plaintext fallback.

Digest связывает решение с immutable kind, wait ID/generation, source ID и
subject (включая schema/origin/исходные аргументы для tool approval). Для
`core_cron_create` он также связывает `resolved_parameters.timezone`; owner UI
показывает его вместе с exact raw arguments, не принимает его как изменение call. Несовпадение
даёт 409 `INTERACTION_VERSION_CONFLICT`; неверный kind или schema — 400.
Повтор того же принятого решения возвращает сохранённое состояние; иное либо
опоздавшее решение — 409 `INTERACTION_CLOSED`. Если deadline уже наступил,
сначала сохраняется timeout. Decision не выдаёт execution lease и не вызывает
инструмент: продолжает задачу обычный recovery coordinator.

Для policy update `expected_origin` является precondition, а не выбранной
клиентом identity. Сервер сам определяет текущий origin; несовпадение даёт 409
`TOOL_IDENTITY_CONFLICT` без изменения policy. Это предотвращает разрешение
другого MCP tool из устаревшей UI-вкладки при повторном использовании alias,
даже если оба origin имеют revision=0. Поле `origin` в запросе запрещено.

### Поток публичного ответа модели

Owner UI и авторизованный внешний A2A-клиент MAY получать текст ответа до
завершения генерации. Поток MUST содержать только публичный ответ адресату
Task: reasoning, tool arguments/results, промежуточный рабочий текст и внутренняя
переписка с владельцами MUST NOT попадать в этот поток. Наличие файлов в потоке
не выводится из workspace: выдаются только явно выбранные scoped результаты.

Модель MAY открыть фазу ответа условным служебным `core_response_begin({})`.
Следующий обычный model turn получает пустой каталог инструментов; сохранённые
инструкции и активированные навыки остаются в контексте. Сигнал расходует обычный
tool budget, следующий turn — обычный model budget. Прежний final text без сигнала
сохраняет buffered поведение; добавочная генерация уже готового текста запрещена.

Публичный preview передаётся стандартным A2A `statusUpdate` со state `working`,
agent Message, правильными `taskId`/`contextId`, накопительным text и metadata
`partial: true`, `core_agent_stream: {version: 1, generation, sequence}`.
Оба номера — положительные целые; generation соответствует списанному model turn,
sequence растёт в её пределах. `superseded: true` с пустым текстом отменяет прежний
preview. Старые, повторные и foreign кадры MUST NOT заменять новый preview.

Preview ограничен по памяти, tenant/Task-scoped и transient. Passive subscription
MUST сначала выдать persisted Task, затем актуальный preview и последующие события;
она не запускает новую работу. Disconnect не отменяет Task, повторное подключение
в том же процессе получает накопленный preview. После рестарта процесса preview
MAY исчезнуть, но durable фаза, usage, Task и workflow continuation сохраняются.
GetTask, история, push и final Artifact остаются авторитетными и не хранят черновик.
Follow-up, retry и отмена сбрасывают устаревший preview на существующей safe boundary.
После первого terminal кадра подписка MUST закрыться без последующих кадров.

UI MUST показывать preview с явной отметкой предварительного ответа и безопасным
plain text: неполные Markdown/Mermaid/code fences не исполняются и не перерисовывают
диаграммы. Каждый partial обновляет текст непосредственно, без GET истории на каждый
фрагмент; canonical polling остаётся механизмом восстановления. Persisted итог
заменяет preview один раз; completed preview остаётся до получения сохранённого
итога, failed/canceled/rejected скрывают его. Обновления сохраняют позицию чтения,
выбор чата, focus и session boundaries. Live preview включается только для
сообщения, принятого в текущем открытом окне. Повторное открытие страницы или
чата MUST использовать сохранённую историю и canonical статусы без восстановления
прежнего preview; новая отправка в этом окне вновь включает live текст. Признак
не сохраняется в browser storage. Временный обрыв подписки без закрытия чата MAY
восстановить preview, внешняя A2A подписка сохраняет описанный выше контракт.

### Безопасный прогресс внешних операций

Root A2A Task MAY содержать metadata `core_agent_remote_progress`: упорядоченный
по local task ID список объектов ровно `{task_id, revision, agent_name,
remote_state}`. `task_id` — локальный handle операции, `revision` — scheduler
revision зафиксированного наблюдения, впервые показавшего текущий status;
одинаковый последующий enum не обновляет её. Значение `remote_state` ограничено
четырьмя non-terminal A2A enum из LONG-02. До первого проверенного Task snapshot
entry отсутствует; при terminal operation/root оно удаляется.
Metadata показывает владельцу и authorized caller прогресс только их root Task.
Remote task/context ID, текст/Parts/история peer, credentials, внутренние HITL
и вопросы владельцам туда не входят. Canonical status root не меняется; ожидание
только remote Task остаётся working. Get/List/Subscribe/push используют одинаковую
safe projection. Повторное чтение не запускает работу и не продлевает deadline.


### Owner schedules API version1

Все routes требуют owner role компании, новую авторизацию каждого HTTP request
и `Cache-Control: no-store`; внешняя и dual-role identity получает403 до lookup.
Tenant, execution owner и origin выводит сервер. Foreign/deleted schedule ID
даёт404 `CRON_NOT_FOUND`; неправильный JSON, неизвестные поля/query, expression,
timezone или request ID —400 `CRON_INVALID`; stale revision или изменённое тело
retry —409 `CRON_CONFLICT`; новый запуск выключенного расписания —409
`CRON_DISABLED`. Delete является tombstone и не отменяет принятую Task.

- `GET /api/schedules`: bounded pagination по `limit`/`cursor`, company-scoped cursor.
- `POST /api/schedules`: `{prompt, expression, timezone?, context_id?, request_id}`;
  default timezone `Europe/Moscow`; отсутствие context создаёт пустой owner chat.
- `GET /api/schedules/{id}`: текущие metadata.
- `PUT /api/schedules/{id}`: `{prompt, expression, timezone, enabled, expected_revision}`;
  enabled —boolean. Edit/re-enable вычисляют next due строго после текущего clock.
- `DELETE /api/schedules/{id}`: `{expected_revision}`; повтор уже применённого
  delete с той же precondition возвращает прежний receipt.
- `POST /api/schedules/{id}/run-now`: `{expected_revision, request_id}`;
  ответ200 `{task}` содержит обычную A2A Task, включая failed/CONTEXT_BUSY.

Metadata содержит `{id, revision, context_id, prompt, expression, timezone,
enabled, next_due_at, active_task_id}`. Даты —ISO-8601 с UTC offset; active task
ID —null, если чат свободен. Disabled schedule имеет next_due_at null. Prompt
проходит действующую root-input validation до любых effects. Все operation
receipts сохраняются company-scoped; retry create возвращает прежний snapshot,
не пересоздавая удалённое расписание/чат. List не раскрывает tombstones.

Chat list включает пустые canonical chats с `latest_task_id: null, active: false`.
Новый chat cursor кодирует context ID; для прежнего длинного context ID,
который не помещается в действующий предел4096 bytes cursor, сервер сохраняет
выдачу legacy task-ID cursor. Старый task-ID cursor читается через
scoped canonical root mapping. История пустого чата доступна без фиктивной Task;
cron skip notices добавляются отдельной owner-only immutable проекцией и
собственным version2 cursor. Version1 transcript cursors продолжают работать.

## Owner API: доступ к агенту (AUTH-04)

Вкладка «Доступ к агенту» показывает число и список внешних сервисных учёток,
название, время выдачи/истечения и состояние pending/active/expired/revoked.
Адрес принимающего A2A берётся из trusted deployment origin, не из client input.
API требует owner authority и отдельные административные права этой же сессии
в configured Keycloak; bearer не предоставляется runtime или модели.

- `GET /api/external-access`: `{accounts: [...], total}`; credentials отсутствуют.
- `POST /api/external-access`: ровно `{name, days, request_id}` (UUID); создание
  или продолжение незавершённого создания. Успех — 201 с `{account, access_token,
  token_type: "Bearer", expires_in}` для однократного показа.
- `POST /api/external-access/{account_id}/token`: ровно `{days}`; новый токен
  той же identity, прежние токены отозваны. Успех — 200 с той же формой.
- `DELETE /api/external-access/{account_id}`: без body/query; native disable,
  200 с `{account}`. Не удаляет A2A results и файлы.
- `DELETE /api/external-access/{account_id}/account`: без body/query; native
  удаление клиента Keycloak вместе с service-account user, 200 с
  `{deleted: true, account_id}`. A2A-задачи/файлы сохраняются для владельцев;
  новая учётка не наследует прежнюю identity. Старый endpoint отзыва не меняется.
  Неизвестная/чужая/имеющая owner authority учётка даёт 404; подтверждённый
  lookup и последующий native DELETE 404 означают конкурентное удаление и успех.

Query parameters и неизвестные/повторённые JSON поля не принимаются. Invalid
name/days/request ID дают 400 REQUEST_INVALID; недоступный чужой account — 404
EXTERNAL_ACCESS_NOT_FOUND; несовпадающий повтор — 409 EXTERNAL_ACCESS_CONFLICT;
завершённое создание — 409 EXTERNAL_ACCESS_ALREADY_ISSUED. Ответ не восстанавливает
потерянный токен; владелец явно выпускает новый для найденной учётки. Keycloak
401/403 даёт 403 KEYCLOAK_ADMIN_ACCESS_DENIED; временная сеть/неверный ответ/timeout
даёт 503 KEYCLOAK_ADMIN_UNAVAILABLE. Все ответы no-store и без upstream body.
Модалка поддерживает native keyboard focus/Escape, copy feedback и закрытие без
сохранения токена; форма блокирует повторное нажатие на время операции.
Кнопки «Отозвать доступ» и «Удалить» независимы. Удаление доступно также для
pending/expired/revoked учёток; подтверждение называет учётку и объясняет
необратимость identity и сохранение данных у владельцев. Отмена не посылает
мутацию; успех убирает строку и обновляет число учёток. При неподтверждённом
исходе UI показывает ошибку и перечитывает список без автоматического DELETE.
