# A2A protocol

## Нормативная база

Основной внешний интерфейс Core Agent MUST соответствовать опубликованной [Agent2Agent Protocol specification](https://a2a-protocol.org/latest/specification/). Реализация фиксирует поддерживаемые A2A protocol versions и объявляет их через Agent Card/capability negotiation; ссылка `latest` используется документацией, но не runtime dependency resolution.

A2A является внешней моделью общения. MCP остаётся протоколом подключения tools/resources, а не transport-ом между клиентом и Core Agent.

## Agent Card

Core Agent публикует public Agent Card и, при наличии закрытых capabilities, authenticated extended Agent Card. Card MUST объявлять:

- A2A protocol version и bindings;
- streaming и push-notification capabilities;
- supported input/output media types;
- authentication schemes;
- public Agent Skills в терминах A2A;
- optional informational extensions, если они объявлены.

A2A Agent Skill описывает внешнюю capability сервера и не равен runtime skill package из RunRequest. Названия могут совпадать, но lifecycle и trust model различаются.

### Версия на каждый binding

Каждый binding MUST объявляться вместе с той protocol version, которую этот endpoint фактически принимает. Card MUST NOT перечислять versions и bindings независимыми списками с последующим декартовым произведением: такая форма выражает пары, которых нет, а объявленная и неподдерживаемая пара является рекламой capability без реализации.

Фактическое сопоставление v1 зафиксировано:

| Binding | Путь | A2A version |
|---|---|---|
| `JSONRPC` | корневой RPC-путь | `0.3` |
| `HTTP+JSON` | REST-пути | `1.0` |

JSON-RPC работает в режиме совместимости `0.3` намеренно: только он даёт ADK-совместимую форму streaming-кадров, описанную ниже. Это осознанный компромисс — ADK-совместимый streaming для v1 важнее единой версии на обоих binding.

Проверка соответствия MUST быть автоматической: для каждой объявленной пары endpoint MUST принимать запрос именно с этой версией. Ручной сверки недостаточно, потому что версия binding задаётся при монтировании routes и расходится с картой незаметно.

Клиент, не приславший `A2A-Version`, обрабатывается как `0.3`. Для REST это означает, что заголовок `A2A-Version: 1.0` обязателен; отсутствие заголовка на REST-пути завершается ошибкой версии, а не молчаливым downgrade.

### Лишние поля JSON-RPC конверта

JSON-RPC 2.0 допускает в конверте ровно `jsonrpc`, `method`, `params` и `id`. Строгая валидация отклоняет запрос целиком при любом лишнем члене верхнего уровня, поэтому клиент, дублирующий поле «на всякий случай» и снаружи, и внутри `params`, не может работать вовсе.

Сервер MUST удалять неизвестные члены верхнего уровня до валидации конверта и обрабатывать запрос по оставшимся четырём. Правило применяется и к batch-массиву поэлементно.

Удалённое поле MUST NOT интерпретироваться. В частности, `contextId` верхнего уровня не становится session id: единственным источником остаётся `params.message.contextId`. Обратное создало бы второй, недокументированный способ задавать сессию.

Факт удаления MUST оставаться наблюдаемым: runtime записывает предупреждение с именами удалённых полей. Повторяющийся набор имён MAY логироваться один раз за процесс, чтобы постоянная ошибка клиента не превращалась в поток одинаковых записей.

Компромисс зафиксирован осознанно. Терпимость к лишним полям облегчает интеграцию, но скрывает ошибку вызывающей стороны: клиент, который кладёт `contextId` **только** снаружи, получит успешный ответ и молча потеряет непрерывность сессии. Именно поэтому предупреждение обязательно, а перекладывание значения внутрь сообщения запрещено — оно замаскировало бы дефект клиента вместо того, чтобы его показать.

### Discovery

Канонический путь публикации — `/.well-known/agent-card.json`.

Тот же документ MUST отдаваться и по историческому пути `/.well-known/agent.json`. Клиенты, каталоги и платформенные регистраторы, написанные до переименования, опрашивают именно его, а `404` там означает не деградацию, а полную необнаруживаемость агента. Оба пути MUST возвращать идентичный документ: исторический путь не является отдельной версией карточки, не расширяет контракт и не может отдавать иной набор capabilities.

Query-параметры при запросе карточки MUST игнорироваться: регистраторы добавляют собственные идентификаторы, и они не влияют на содержимое ответа.

### Advertised URL

Карточка MUST объявлять адрес, по которому удалённый caller действительно может обратиться к агенту. Loopback-адрес вида `http://localhost:{PORT}` таким адресом не является ни при каком развёртывании за прокси: обнаружение формально работает, агент выглядит живым, а вызов завершается отказом соединения. Это молчаливый отказ, поэтому адрес не может оставаться на усмотрение оператора.

Порядок определения адреса фиксирован:

1. `AGENT_URL`, если задан, является authoritative и MUST использоваться без изменений. Никакой заголовок запроса не может его переопределить. Runtime MUST принимать то же значение под именем `URL_AGENT`: платформы развёртывания публикуют публичный адрес агента под обоими написаниями, а перестановка двух слов в имени переменной ничем не отличается от незаданной переменной и приводит к карточке с недостижимым адресом. При обоих заданных значениях выигрывает `AGENT_URL` как документированное имя.
2. Иначе адрес MUST выводиться из заголовков конкретного запроса карточки: `X-Forwarded-Proto` и `X-Forwarded-Host`, а при их отсутствии — схема запроса и `Host`.
3. Если пригодного значения нет, используется статический адрес из конфигурации. Запрос карточки MUST завершаться успешно: недоступность адреса не повод отвечать ошибкой на discovery.

Выведенное значение проходит валидацию до попадания в карточку: из списка через запятую берётся первый элемент, значение MUST NOT содержать пробельные и управляющие символы, MUST NOT содержать userinfo, а схема MUST быть `http` или `https`. Непрошедшее проверку значение отбрасывается в пользу шага 3.

Компромисс зафиксирован осознанно: `Host` контролируется вызывающей стороной, поэтому выведенный адрес достоверен ровно настолько, насколько доверенным является прокси перед агентом. Production SHOULD задавать `AGENT_URL` явно. Ограничение обязательно: выведенный адрес MUST использоваться только как advertised URL карточки и MUST NOT влиять на authentication, policy, tenant resolution или исходящие запросы агента.

## Входы запроса

A2A несёт единственный логический вход:

- `prompt` — content A2A `Message.parts`;
- session — A2A `contextId`;
- запуск/фоновая работа — A2A `Task`;
- результат — A2A `Artifact`;
- пользовательское объяснение или запрос данных — A2A `Message`.

Core Agent MUST NOT требовать собственного расширения протокола для обычного запуска. Любой стандартный A2A-клиент является валидным клиентом: отсутствие расширений в запросе не является ошибкой.

MCP-серверы и skills задаются конфигурацией развёртывания и описаны в [Публичном контракте](public-contract.md). Прежнее расширение `urn:core-agent:run-capabilities:v1` удалено вместе с требованием его объявлять; правила обратной совместимости определены там же.

Agent Card MAY объявлять optional informational extensions, не влияющие на приём запроса. Такое расширение MUST NOT быть required и MUST NOT блокировать клиента, который о нём не знает.

## Task mapping

| Core state | A2A Task state | Дополнительная семантика |
|---|---|---|
| `CREATED`, `QUEUED` | `submitted` | задача принята, worker ещё не выполняет turn |
| `RUNNING`, `WAITING_TASK`, `PAUSED`, `RECOVERING` | `working` | точная причина доступна в безопасном status metadata |
| `WAITING_INPUT` | `input-required` | caller должен предоставить новые бизнес-данные |
| `WAITING_AUTH` | `auth-required` | требуется credential/auth flow |
| `COMPLETED` | `completed` | результаты представлены Artifacts; `completion_reason=budget_exhausted` и `complete=false` явно помечают честный неполный результат |
| `FAILED`, `ABORTED` | `failed` | error metadata различает обычную ошибку и unsafe continuation |
| `CANCELLED` | `canceled` | отмена подтверждена runtime |
| `REJECTED` | `rejected` | policy отказала до выполнения |

Internal state не добавляет новые A2A terminal states. Client, понимающий только стандартный A2A, остаётся корректным.

Budget exhaustion не добавляет отдельного A2A state и не отображается в
`failed`: Task завершила разрешённую работу и публикует partial Artifact со
stable metadata `completion_reason: "budget_exhausted"`, `complete: false` и
exhausted dimension. Текст Artifact MUST явно говорить, что objective выполнен
не полностью. Полное завершение использует `completion_reason: "completed"` и
`complete: true`.

Artifact metadata и при live completion, и при recovery имеет одинаковую форму:
top-level `digest`/`size` и вложенный `provenance` с completion fields, локальным
`usage`, общим `shared_budget` и optional `pending_tasks`.

## Операции

Core Agent MUST поддерживать A2A operations, необходимые для:

- send message: начать или продолжить task;
- send streaming message: получить status/artifact updates в реальном времени;
- get/list task: polling и управление несколькими фоновыми tasks;
- subscribe to task: восстановить stream активной task;
- cancel task;
- create/get/list/delete push notification configuration;
- получить extended Agent Card, если capability объявлена.

Конкретный binding MAY временно поддерживать подмножество optional operations только если Agent Card честно отражает capability.

После потери process-local stream новая подписка является пассивным наблюдением,
а не командой resume. Она сначала публикует сохранённый Task, затем только его
durable изменения и не снимает `WAITING_*`, `PAUSED` или `auth-required`.
Безопасные root workflow продолжает recovery coordinator; terminal update не
может предшествовать начальному снимку или сопровождаться более поздним
`working`-кадром. Подписка на process-local active Task также начинает с уже
прочитанного persisted snapshot и прекращается сразу после первого terminal
события: никакой artifact/status frame не публикуется после terminal.

## Сообщения активной Task

Caller MAY отправлять дополнительные A2A Messages в уже существующую non-terminal Task, указывая её server-generated `taskId`. Если передан `contextId`, он MUST совпадать с context Task; `contextId` без `taskId` создаёт новую Task и не steer-ит существующую. После terminal state Message отклоняется стандартной A2A terminal-task ошибкой.

Сервер MUST:

- проверить authenticated caller, tenant и доступ к Task с not-found semantics для чужого ID;
- принимать от caller только `ROLE_USER` и нормализовать его в новый user turn с provenance исходного `messageId`;
- сохранить Message в durable inbox до acknowledgement, назначить monotonic task-local sequence и дедуплицировать повторный `messageId`;
- вернуть ту же Task, а не создать параллельный run;
- доставить принятые Messages в commit order на ближайшей safe boundary перед следующим model turn;
- не прерывать уже начатый model call, tool call или side effect;
- перед terminal commit атомарно проверить inbox: Message, committed раньше успешного terminal transition, MUST быть доставлен модели; перед `failed`/`canceled` он MUST быть durable перенесён в transcript с явной причиной, что остался необработанным, без нового model/tool call; Message, проигравший race terminal transition, MUST быть отклонён.

Follow-up не пересобирает EffectiveConfig и не пополняет budgets: capabilities принадлежат конфигурации и неизменны на протяжении Task. Новейший turn MAY уточнить или изменить желаемый будущий результат, но не отменяет уже committed side effect; для отмены Task используется `CancelTask`.

`WAITING_INPUT` и `WAITING_TASK` Task возобновляются notification-ом о новом inbox Message. В `RUNNING` Message ждёт следующую safe boundary. В `PAUSED` и `WAITING_AUTH` Message сохраняется, но не снимает паузу и не передаётся модели до разрешённого resume.

## Blocking, background и ожидание

- Простое взаимодействие MAY вернуть direct A2A Message.
- Любая работа с tools, side effects, background execution или сабагентом MUST иметь Task.
- Non-blocking запуск использует A2A `return_immediately`; клиент затем polling, subscription или push notification.
- Закрытие streaming connection MUST NOT отменять Task.
- Агент MAY перейти в пассивное `WAITING_TASK`; это остаётся A2A `working`, не потребляет model/CPU и возобновляется notification-ом.
- Critical state не полагается только на transient Message: она сохраняется в Task status/history или Artifact.
- Terminal reconciliation после recovery атомарно обновляет durable A2A Task и
  ставит настроенное push notification в idempotent delivery ledger. Ошибка
  любой из этих записей откатывает обе и повторяется последующей сверкой.
  Восстановленный `failed`/`rejected` status содержит тот же безопасный stable
  error code, что и live terminal path, без raw exception или чувствительных
  деталей.
- Потеря workflow lease и graceful shutdown worker-а не публикуют `failed` или
  иной terminal A2A state. Stale process прекращает локальный producer, а durable
  итог публикует действующий lease-owner либо последующая reconciliation. Если
  workflow ещё не был durable создан, adapter вместо этого публикует безопасный
  `failed`: невосстановимая A2A Task не остаётся навсегда в `working`.
- Если `CancelTask` пересекается с уже сохранённым terminal workflow, adapter не
  заменяет этот итог на `failed`: локальный producer публикует свой сохранённый
  результат, а при отсутствии локального producer adapter восстанавливает его из
  durable workflow. Внутренняя координация отмены не накапливает process-local
  записи для чужих или уже неактивных Task.

## Artifacts и Messages

- Итоговые и промежуточные результаты задачи публикуются как Artifacts с content parts, media type, digest и provenance.
- Messages используются для общения, progress summary и remote input request.
- Partial artifact updates идемпотентны и имеют stable artifact ID/version.
- Secret, hidden reasoning и raw sensitive tool output MUST NOT попадать в Message или Artifact без явной data policy. Provider-visible reasoning MAY появляться только как помеченная часть промежуточного status Message при включённом streaming и MUST отсутствовать в терминальном кадре и Artifact.

## Streaming прогресса выполнения

Streaming показывает вызывающей стороне ход выполнения Task: рассуждение модели, вызовы инструментов, их результаты и растущий текст ответа. Формат кадров фиксирован, потому что клиенты собирают из него состояние.

### Binding

Сервер MUST монтировать оба binding: JSON-RPC на корневом RPC-пути и HTTP+JSON REST. ADK-совместимую форму кадров (`kind`, `final`, lowercase `state`, части `kind: "text"|"data"`) даёт JSON-RPC binding в режиме совместимости A2A 0.3; она и является нормативной для этого раздела. Agent Card объявляет оба binding.

### Последовательность кадров

Один streaming-вызов MUST давать ровно такую последовательность:

1. один кадр `kind: "task"` с начальным состоянием;
2. ноль или больше кадров `kind: "status-update"` со `state: "working"` и `final: false`;
3. ноль или больше кадров `kind: "artifact-update"` с чанками результата;
4. ровно один терминальный кадр `kind: "status-update"` с терминальным `state` и `final: true`.

Кадр с `final: true` MUST быть последним. Ни один промежуточный кадр MUST NOT иметь `final: true`.

### Типы промежуточных кадров

Промежуточный `status-update` несёт agent-сообщение, части которого различаются по метаданным:

| Часть | Метаданные части | Содержимое |
|---|---|---|
| text | `adk_thought: true` | provider-visible reasoning |
| text | отсутствуют | публичный текст ответа |
| data | `adk_type: "function_call"` | `{"id", "name", "args"}` |
| data | `adk_type: "function_response"` | `{"id", "name", "response"}` |

`response` внутри `function_response` содержит `status` (`succeeded`, `failed`, `denied` или `timed_out`), `output` и, для неуспешного исхода, `error_code`.

Кадр с текстом MUST нести `partial: true` в metadata сообщения. Кадры `function_call` и `function_response` MUST NOT быть partial: они являются дискретными фактами, а не снимками.

Отклонение от исходной ADK-схемы: признак `partial` передаётся в `message.metadata`, а не отдельным полем `Message`, потому что используемые SDK-типы игнорируют неизвестные поля верхнего уровня. Прочие поля и метки совпадают.

### Кумулятивные снимки

Текстовый кадр является полным снимком, а не приращением: клиент MUST заменять ранее показанный текст, а не дописывать его. Снимок содержит обе накопленные строки — reasoning и публичный ответ.

Слияние приходящих от provider фрагментов MUST быть консервативным: фрагмент, начинающийся с уже накопленного буфера, заменяет буфер целиком, иначе он дописывается. Догадки о частичном перекрытии запрещены — они молча удаляют символы из легитимного приращения.

Новый снимок публикуется, только когда хотя бы один из двух каналов вырос не меньше чем на `A2A_STREAMING_BUFFER_SIZE` символов. Накопленный остаток MUST принудительно публиковаться на двух границах: по завершении model turn и непосредственно перед кадром `function_call`. После `function_call` буфер сбрасывается, поэтому текст следующего turn начинается с нуля и не дублирует предыдущий.

### Терминальный кадр и ошибки

Терминальный кадр несёт итоговый текст ответа. Provider-visible reasoning MUST отсутствовать в терминальном кадре и в Artifact.

Результат публикуется как Artifact, разбитый на чанки по `MAX_CHUNK_SIZE` символов; `0` означает один чанк. Только последний чанк помечается `lastChunk`.

При сбое сервер MUST опубликовать терминальный `failed`-кадр, содержащий уже отданный текст, разделитель и безопасный код ошибки, чтобы клиент не остался с оборванным потоком без объяснения. Если терминальное состояние уже опубликовано, повторная публикация не выполняется.

### Durable history и push

Промежуточные снимки являются транспортным прогрессом, а не durable-состоянием:

- agent-сообщения с `partial` MUST удаляться из `Task.history` перед сохранением, иначе история переписывалась бы квадратично растущим объёмом;
- push notification MUST NOT отправляться для таких кадров.

Оба правила MUST проверять роль сообщения и применяться только к `agent`. Клиент не может подделать `partial` в собственном сообщении и тем самым удалить свой turn из durable истории.

Кадры `function_call`, `function_response`, терминальные и не-agent сообщения сохраняются и доставляются push обычным образом.

### Порядок, back-pressure и отключение

Agent loop синхронный; каждая публикация MUST переноситься на serving event loop и дожидаться постановки в очередь. Это сохраняет порядок кадров и передаёт производителю back-pressure очереди. Публикация после терминального состояния тихо прекращается и не является ошибкой.

`A2A_STREAMING_ENABLED=false` полностью отключает промежуточные кадры: остаются только начальный, artifact и терминальный. Ни один другой контракт при этом не меняется.

## Notifications

A2A streaming, polling и push notifications являются тремя представлениями одной durable task history. Push delivery MUST быть at-least-once; receiver обрабатывает update идемпотентно по task ID, artifact/status version и event sequence.

Webhook implementation MUST проверять HTTPS/authentication, защищаться от SSRF и не разрешать private/loopback target без явной host policy.

Production deployment MUST задать `PUSH_NOTIFICATION_ENCRYPTION_KEY` как URL-safe base64 Fernet key из secret injection. Push configurations и authentication tokens хранятся в PostgreSQL только в зашифрованном виде. Каждая доставка сначала фиксируется в durable delivery ledger с уникальностью `(task_id, config_id, event_key)`, затем отправляется с stable delivery ID. Успешный HTTP response фиксируется после отправки; crash между send и commit приводит к допустимой повторной доставке. Pending delivery восстанавливается после restart, использует bounded exponential backoff и не теряется после исчерпания фиксированного числа попыток.

Target URL проверяется и при регистрации, и непосредственно перед каждой доставкой: только HTTPS port 443, без userinfo и redirect following; все DNS addresses должны быть public/global. Unresolved, loopback, link-local, private, reserved и mixed public/private answers отклоняются. Явное разрешение private targets требует отдельной host policy, отсутствующей в v1.

## Trace context

A2A binding MUST принимать и передавать W3C Trace Context через разрешённые transport headers. Trace context не используется для authorization. Baggage по умолчанию не пересылается внешнему агенту; allowlist запрещает secrets, prompt и user content.

## Версионирование

- A2A protocol version договаривается стандартным способом binding-а; runtime фиксирует `Major.Minor`, а patch не участвует в compatibility negotiation.
- Новый optional extension field обратно совместим; новый required field требует новой major extension version.
- Persisted Task хранит A2A и extension versions, с которыми он был создан.
