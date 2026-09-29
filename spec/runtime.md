# Runtime и reasoning

## Состояния запуска

Полная state machine определена в [Архитектуре](architecture.md). Runtime MUST поддерживать durable ожидание input, pause/resume и recovery, а не удерживать worker или model connection открытыми.

Переход в терминальное состояние необратим.

## Инициализация

До первого вызова модели ядро MUST:

1. провалидировать входящий A2A Message;
2. вычислить immutable admission ceiling из PlatformConfig, tenant policy,
   AgentConfig, Task capabilities и MCP declarations; текущий deny впоследствии
   может только сузить его;
3. создать `run_id`, event stream и начальный checkpoint с admission ceiling и
   сроком MCP discovery;
4. разрешить MCP descriptors через admission policy и выполнить discovery;
5. вычислить и зафиксировать immutable EffectiveConfig и полный проверенный MCP
   catalog со schemas;
6. разрешить skills и создать их lock snapshot;
7. построить system/kernel/capability instructions;
8. загрузить session working state и, если разрешено, вызвать retrieval MCP и подсистему памяти;
9. выбрать primary model route и проверить capabilities;
10. вычислить доступный контекстный бюджет;
11. перевести A2A Task в `working` и испустить внутреннее `task.started`.

Если шаг не выполнен, модель и инструменты MUST NOT вызываться.

## Состав системных инструкций

Ядро формирует инструкции в следующем порядке приоритета:

1. неизменяемые safety-инварианты платформы;
2. PlatformConfig, AgentConfig/EffectiveConfig и host policy;
3. versioned KernelInstructions;
4. настраиваемый AgentProfilePrompt;
5. исходный пользовательский prompt/A2A Message;
6. инструкции активированных skills;
7. retrieved MCP results и transcript;
8. данные, полученные от tools/MCP/A2A peers.

Нижний уровень MUST NOT отменять верхний. Memory protocol, обязательные tools, delegation rules, task lifecycle, TerminalSession ownership/lifecycle и observability принадлежат KernelInstructions и одновременно enforced runtime-ом. AgentProfilePrompt не может их заменить. Полный contract описан в [Kernel instructions](kernel-instructions.md).

Если prompt и skill противоречат друг другу без нарушения уровней 1–3, явный prompt имеет приоритет. Если безопасное разрешение неоднозначно, агент запрашивает уточнение через итоговый ответ, не угадывает.

## Agent loop

В состоянии `RUNNING` ядро повторяет:

1. принимает durable task notifications и новые inbound Messages на safe boundary;
2. проверяет разрешённость материала guardrails и добавляет каждый принятый Message отдельным user turn с provenance и committed sequence; отклонённый материал заменяет уведомлением без содержимого;
3. оценивает заполнение контекста и при необходимости выполняет compaction;
4. вызывает модель с активным контекстом и доступными tool schemas;
5. стримит пользовательский текст, вызовы инструментов и их результаты; provider-visible reasoning помечается отдельной частью, скрытый reasoning не передаётся;
6. если модель завершила ответ — атомарно проверяет inbound inbox и завершает запуск только при отсутствии более раннего unread Message;
7. если модель запросила tool — валидирует имя и arguments;
8. проверяет текущую tool policy, guardrails и при необходимости durable ждёт owner HITL;
9. исполняет разрешённый tool, нормализует result и добавляет его в контекст;
10. продолжает цикл, другую независимую работу или passive wait.

Последовательность является базовой семантикой. Runtime MAY построить dependency graph и параллельно исполнить доказуемо независимые read-only calls или изолированные child Tasks. Каждый call всё равно получает отдельные policy decision, lifecycle и audit. Порядок слияния результатов должен быть стабильным.

## Live steering

Новый A2A Message не является interrupt текущей операции. Adapter durable-фиксирует его в inbox существующего non-terminal run; orchestrator забирает committed Messages перед следующим model turn. Если Message приходит между последним model response и terminal commit, completion gate обязан либо включить его в следующий turn, либо проиграть race уже committed terminal state и отклонить SendMessage. При `failed` или `canceled` принятый раньше Message не запускает новый model/tool call: runtime атомарно переносит его в transcript с явной пометкой `unprocessed_due_to_failure` или `unprocessed_due_to_cancel`, затем повторяет terminal commit.

Каждый accepted Message сохраняет исходные `messageId`, task/context IDs, authenticated caller provenance и monotonic inbox sequence, но поступает модели как недоверенный user-role input. Несколько Messages не склеиваются в один prompt и не подменяют system/kernel instructions. Unconsumed Messages pinned при compaction и recovery. Follow-up не сбрасывает и не увеличивает hard budgets.

Повод пробуждения определяется типом durable wait. Follow-up немедленно закрывает только ожидание `core_wait_until` с причиной прихода сообщения. Во время HITL, вопроса владельцу, guardrails или remote wait он сохраняется и доставляется после разрешённого завершения ожидания, не меняя deadline/полномочия. В `PAUSED`/`WAITING_AUTH` сообщение не снимает host pause или auth flow. Timer, решение и сообщение конкурируют атомарно за одно продолжение.

## Reasoning

- Ядро MUST использовать reasoning-capable режим модели, если он поддерживается выбранным provider.
- Deployment MAY задать `THINKING_LEVEL`; отсутствие значения оставляет provider default. OpenAI-compatible adapter передаёт его как `reasoning_effort`, Anthropic Messages — как `output_config.effort` с adaptive thinking. Неподдерживаемое provider-ом значение завершается обычной model error, а не silent downgrade.
- Уровень reasoning и model route являются Platform/Agent configuration: remote A2A caller не управляет ими через RunRequest, prompt, MCP или skill.
- Provider-returned visible reasoning/summary не является публичным ответом и не попадает в audit, memory или tool arguments. В A2A stream оно MAY публиковаться только как отдельная помеченная часть при включённом streaming и MUST вырезаться из терминального кадра и Artifact. В operator logs/traces оно появляется только при explicit content capture, после redaction и truncation.
- Provider-hidden chain-of-thought, encrypted/redacted thinking, signatures и другие opaque replay data MUST NOT попадать в operator telemetry. Adapter MAY сохранить минимальный provider-native replay внутри owned workflow context, когда это требуется для продолжения tool conversation.
- Известное число reasoning tokens является usage metadata и MAY экспортироваться без reasoning text.
- Ядро MAY отдавать краткое резюме намерения или основания действия, сформулированное для пользователя.
- Отсутствие обязательной capability MUST быть известно до запуска и либо компенсировано разрешённым fallback, либо завершаться `MODEL_CAPABILITY_MISSING`.

## Model routing и fallback

Route описывается требованиями, а не именем модели: context window, modalities, tool calling, reasoning, structured output, region, data policy, latency и price ceiling.

- Provider adapter MUST объявлять capabilities и effective limits.
- Provider adapter MUST сохранять нативную последовательность user message → assistant tool call → tool result с исходным `tool_call_id`; склейка tool result обратно в новый user prompt запрещена, потому что ломает provider tool protocol и провоцирует повторный вызов.
- Fallback MUST сохранять system/user semantics и tool call state.
- Переход на модель с меньшим окном требует compaction до вызова.
- Provider не может получить данные, запрещённые tenant routing policy.
- После начала mutating tool protocol fallback не должен повторять уже принятый tool call.
- Смена route отражается событием с безопасной причиной, provider/model MAY быть скрыты host policy.

## Решения владельцев и запрос информации

Tool HITL, вопрос владельцу и решение guardrails имеют разные типы durable request. Runtime сохраняет тип, source/call ID, immutable arguments/material version, deadline и continuation до публикации в UI. Обычный follow-up не может стать разрешением или owner answer. Решение проверяет authenticated owner role на отдельной границе; schema, ceiling и актуальная tool policy проверяются вновь перед dispatch.

### HITL-01. Запрос и решение

- Запрос связан с чатом, задачей и конкретным вызовом с конкретными аргументами.
- Владелец видит, какое действие и с какими аргументами запрашивается.
- Доступны только «Разрешить» и «Отклонить»; редактирования аргументов нет.
- Решение передаётся через отдельный endpoint с повторной проверкой роли.
- Внешние агенты не могут принимать решения даже по собственным задачам.
- До разрешения защищаемый вызов не выполняется.
- Решение и продолжение сохраняются durable; браузер не обязан оставаться открыт.

### HITL-02. Отказ и timeout

- Отказ возвращается модели как результат вызова инструмента.
- Агент может продолжить решение в рамках разрешённых возможностей.
- Таймаут настраивается в UI, значение по умолчанию — 24 часа.
- По timeout возвращается результат «подтверждение не получено вовремя»;
  вся задача автоматически не завершается.
- Абсолютный срок сохраняется и не начинается заново после restart.

Первое зафиксированное решение либо timeout закрывает
запрос; повторная доставка решения идемпотентна. Одобрение и последующий dispatch
должны повторно проверять актуальную policy.

### HITL-03. Переход инструмента в автоматический режим

Если вызов уже ожидает HITL, перевод инструмента в автоматический режим
не разрешает этот вызов автоматически и не закрывает запрос владельцам.
Сохраняются исходные аргументы и прежний deadline. Вызов продолжает ждать
явного решения владельца либо завершается по прежнему правилу timeout.

Новые вызовы после изменения настройки используют автоматический режим,
в том числе в уже активных задачах других чатов. Настройка не отменяет
schema validation, остальные ограничения policy или проверки guardrails.

Перед исполнением ранее ожидавшего вызова после разрешения владельцем
повторно проверяются текущие ограничения: сохранённый HITL-запрос не позволяет
выполнить инструмент, который к этому моменту запрещён.

### HITL-04. Запрет инструмента во время ожидания

При переводе инструмента в запрещённый режим все ожидающие HITL вызовы этого
инструмента во всех чатах сразу завершаются без исполнения. Модель получает
структурированный результат «инструмент запрещён» и может продолжить задачу
с доступными возможностями. Это отказ policy, а не решение владельца
«Отклонить» по конкретному HITL-запросу; сам запрет не переводит задачу в failed.

Закрытие запроса и повод продолжения сохраняются durable. Позднее разрешение,
timeout или restart не открывают закрытый запрос и не запускают продолжение
повторно. Последующее включение инструмента действует для новых вызовов
и не восстанавливает закрытые. При гонке запрета с разрешением перед dispatch
проверяется актуальная policy; уже переданный на исполнение вызов подчиняется
правилу TOOL-01.

### INPUT-01. Вопрос владельцу

Агент может обратиться к владельцам за недостающей информацией и при работе над
задачей внешнего агента. Вопрос и ответ относятся к соответствующему чату;
внешний caller не получает прав владельца. Вопрос со свободным текстовым ответом
отличается от HITL, где доступны только разрешение и отказ.

При обращении к владельцам внешний агент видит только факт ожидания, например
«Ожидается ответ владельца», а после выполнения — итоговый ответ по задаче.
Тексты вопросов и ответов владельцев остаются во внутренней переписке, доступной
владельцам. Внешний caller не может ответить от имени владельца; его обычный
follow-up не закрывает ожидание ответа владельца.

Ограничение действует для всех внешних представлений этой Task: истории,
GetTask/ListTasks, потоковых событий, push-уведомлений, файлов, артефактов и
metadata. Нельзя раскрывать внутреннюю переписку через другой A2A-метод или
публикацию внутреннего transcript/summary. Агент использует полученные сведения
для решения задачи и формирования предназначенного внешнему caller-у итогового
ответа; саму переписку с владельцами автоматически в ответ не копирует.
Это правило дополняет изоляцию внешних агентов и действует даже внутри задачи,
принадлежащей данному caller-у.

Ожидание ответа владельцев имеет настраиваемый timeout, по умолчанию 24 часа.
Абсолютный срок сохраняется вместе с запросом и не начинается заново после
перезапуска или поступления follow-up. По истечении срока ожидание завершается,
а агент получает структурированный результат «ответ не получен вовремя» и может
продолжить задачу без этих сведений. Сам timeout не переводит задачу в failed;
агент не должен подменять отсутствующий ответ вымышленными данными.

Ответ владельца и истечение срока конкурируют за одно завершение ожидания:
возобновление происходит однократно. Сохранённые за время ожидания follow-up
доставляются перед следующим обращением к модели по общим правилам.
Конкретные имя инструмента и API-схема определяются при технической проработке
контракта; поведение запроса информации согласовано.

## Фоновые задачи и делегирование

Long-running tool, indexing job и сабагент запускаются как неблокирующие Tasks. Parent получает handle, продолжает другую работу или durable-переходит в `WAITING_TASK`. Completion notification возобновляет loop и добавляется в контекст на safe boundary. Delegation MUST передавать явные allowlists tools/MCP/skills; детали определены в [Фоновых задачах и делегировании](tasks-and-delegation.md).

## Завершение

Агент завершает запуск, когда:

- задача выполнена и сформирован итоговый ответ;
- дальнейшая работа остановлена hard budget, а сохранённый финальный turn
  сформировал честный промежуточный ответ;
- безопасное продолжение требует новых пользовательских данных;
- после human-input timeout модель сформировала итог по доступным данным; сам timeout возвращает structured result и не terminalize-ит Task;
- произошла невосстановимая ошибка;
- получена команда отмены.

Наличие pending background task не требует держать run активно вычисляющимся. Agent MAY ничего не делать и ждать. Terminal completion допускается только если pending tasks отменены, detached по policy или явно не нужны результату.

Policy deny не является автоматической ошибкой: tool result с отказом возвращается модели, чтобы она могла выбрать альтернативу или объяснить блокировку.

## Budgets и защита от зацикливания

PlatformConfig/AgentConfig MUST задавать hard limits минимум для:

- числа model turns;
- числа tool calls;
- wall-clock времени;
- стоимости или токенов, если provider даёт такую метрику;
- размера одного tool result и суммарного artifact storage.
- depth/fan-out и суммарного бюджета child Tasks;
- числа compaction/retrieval операций;
- времени ожидания человека отдельно от active compute time.

Эффективный лимит model turns MUST быть не меньше `1`, потому что этот ход
является обязательным финализирующим reserve. Меньшее значение отклоняется как
`CONFIG_INVALID` до создания workflow или budget ledger.

При достижении 90% любого hard limit ядро SHOULD дать модели сигнал завершить
задачу кратчайшим безопасным способом. Runtime MUST заранее удержать внутри
общего parent budget один model turn и соответствующий output reserve для
финализации каждого принятого run. Этот reserve учитывается в hard limit и не
является автоматическим увеличением бюджета; рабочие model calls не могут его
занять. Если budget допускает только один turn, run сразу выполняет финализацию
без рабочих tools.

Если run завершился обычным полным ответом и удержанный turn не использовал,
runtime MUST вернуть reserve в общий ledger атомарно с terminal transition.
Последовательные короткие child Tasks не должны исчерпывать parent budget только
из-за уже ненужных резервов завершённых children.

Когда следующий рабочий model call превысил бы лимит, runtime использует
удержанный turn с пустым tool catalog и protected instruction вернуть только:

- уже проверенный промежуточный результат;
- действия, которые модель собиралась выполнить, но не успела;
- явное утверждение, что задача не завершена полностью.

Модель MUST NOT достраивать отсутствующие tool results, выдавать намерение за
выполненное действие или продолжать работу в финализирующем turn. Если модель не
вернула пригодный text либо после разрешённых retry вернула `MODEL_UNAVAILABLE`,
runtime создаёт только детерминированное безопасное сообщение об исчерпанном
budget и отсутствии финальной сводки; hidden reasoning и raw tool output не
становятся публичным результатом.

Follow-up, принятый во время уже выполняющегося финализирующего turn, сохраняется
и доставляется в transcript, но не создаёт второй model call сверх hard limit.
Terminal partial result детерминированно сообщает, что этот follow-up записан,
но не обработан из-за исчерпанного budget; missing outcome не достраивается.

Когда следующий tool call превысил бы локальный или общий parent tool budget,
runtime MUST NOT dispatch-ить его. Каждый ещё не выполненный tool request из
того же assistant message получает обычный structured failed tool result с
`BUDGET_EXCEEDED`, dimension, фактическим usage, limit и instruction немедленно
передать проверенный промежуточный результат выше. Эти synthetic outcomes не
являются tool dispatch, retry или новым side effect. После них используется
удержанный финальный model turn без tools.

Исчерпание execution budget само по себе MUST NOT переводить run в `FAILED` и
MUST NOT уничтожать уже committed context, Artifacts или child results. Run
терминально переходит в `COMPLETED`, а persisted result содержит
`completion_reason: "budget_exhausted"`, `complete: false`, исчерпанную dimension
и фактический usage. Обратно совместимое поле `usage` содержит локальные
счётчики model loop этого run. Поле `shared_budget` содержит атомарный snapshot
общего root ledger после возврата неиспользованного terminal reserve:
`scope: "root"`, `used` и `limits` для model turns и tool calls. Обычное полное завершение содержит
`completion_reason: "completed"`, `complete: true`. Это не разрешает скрывать
неполноту: text result обязан явно отделять выполненное от незавершённого.

Перед budget finalization runtime запрашивает cancel всех owned Tasks и ждёт их
не дольше одного bounded cancellation grace. Не ответивший на cancel worker не
получает ложный terminal status и не блокирует partial result бесконечно: его
durable row сохраняет `cancel_requested`, а persisted result перечисляет такой
task ID в `pending_tasks` и прямо говорит, что отмена/outcome не подтверждены.
Поздний terminal outcome остаётся в task row, mailbox и outbox. Неиспользованный
finalization reserve отменённого child возвращается общему ledger атомарно с
его `CANCELLED` transition; reserve уже начатого finalizer не возвращается.

Hard limits остаются абсолютными: runtime не начинает вызов, для которого не
зарезервирована ёмкость, не сбрасывает usage и не увеличивает budget. Если
outcome уже начатой мутации неизвестен, `SIDE_EFFECT_UNKNOWN` и reconciliation
имеют приоритет над частичным завершением по budget.

Поля result являются аддитивной persisted metadata и не меняют RunRequest или
A2A protocol version. При чтении результата, записанного прежней версией без
`completion_reason`/`complete`, runtime MUST трактовать его как обычное полное
завершение (`completed`/`true`). JSON-хранилища не требуют data migration.

## Повторные попытки

- Read-only и идемпотентные provider/MCP операции MAY повторяться при временной ошибке с ограниченным exponential backoff. MCP initialize/discovery при cold start начинается после durable создания workflow и immutable admission snapshot, ограничивается единым для всех MCP данного run `MCP_COLD_START_TIMEOUT_SECONDS`, прерывается отменой и не расходует model/tool budget. Срок ожидания сохраняется в checkpoint и не начинается заново после recovery; reconnect уже инициализированного workflow получает отдельный сохраняемый срок, но не меняет его EffectiveConfig.
- Мутирующий tool call MUST NOT повторяться автоматически, если нет достоверного idempotency key или подтверждения, что действие не началось.
- Каждая попытка сохраняется в аудите под одним логическим `tool_call_id` и отдельным `attempt`.

## Pause, recovery и lease

- `pause` запрещает новые model/tools после ближайшей безопасной границы и создаёт checkpoint.
- Worker MUST регулярно обновлять lease, в том числе во время долгого model/tool
  call и joined delegation; только владелец актуального lease изменяет run.
- Recovery MUST пропускать workflow с ещё действующим lease и само получать
  новый fenced lease перед reconciliation; совпадение worker ID не разрешает
  заменить живой token. Истёкший token не может быть продлён или записать
  terminal transition даже до того, как его забрал другой worker. PostgreSQL
  вычисляет и сравнивает expiry по текущему времени сервера БД в момент fenced
  write после возможного ожидания row lock, а не по часам replica или времени
  начала SQL statement. Поэтому clock skew и lock wait не сокращают и не
  продлевают fencing window. Workflow transition повторно проверяет token и
  expiry на финальном `core_runs` write после всех budget/outbox/audit locks;
  ранняя проверка перед потенциальной блокировкой не является fencing.
- Recovery coordinator MUST автоматически повторно сканировать durable workflow,
  чтобы продолжить `RUNNING`/`MODEL_RESPONDED` после истечения старого lease без
  resubscribe или иного запроса клиента. Пассивные `WAITING_*`, `PAUSED` и
  `APPROVED_RESERVED` при таком сканировании не запускаются. Фильтр root workflow
  применяется до batch limit: очередь child workflow не может вытеснить root из
  каждого сканирования. Если claim или continuation получает `LEASE_LOST`, одна
  попытка recovery завершается без локального busy polling: право следующей
  попытки определяет очередной scan после истечения или освобождения lease.
- Первичный root admission MUST атомарно сохранить workflow и lease запускающего
  worker: recovery не может перехватить новую запись в зазоре между admission и
  началом `_continue_workflow`. Child workflow принадлежит durable scheduler и не
  запускается общим root recovery coordinator.
- A2A cancel сначала durable-фиксирует `cancel_requested`, не объявляя отмену
  завершённой. Только текущий владелец fenced lease либо recovery после истечения
  lease переводит workflow в `CANCELLED`; `EXECUTING` с неизвестным outcome
  остаётся reconciliation. Cancel, принятый до создания workflow row, не может
  теряться из-за локального timeout и применяется сразу после durable admission.
  Принятый intent имеет приоритет над более поздними `COMPLETED`, `FAILED` и
  `REJECTED`; владелец lease завершает такой non-ambiguous workflow как
  `CANCELLED`.
- Graceful shutdown worker-а не является A2A cancel: он останавливает текущие и
  ожидающие локальные continuation, не записывает terminal state и оставляет
  workflow для следующего fenced recovery. `LEASE_LOST` также является fencing
  signal, а не ошибкой продукта: stale worker прекращает публикацию и не переводит
  durable workflow или внешнюю A2A Task в terminal state. Это правило действует
  только после durable admission: остановка до создания workflow возвращает
  безопасную terminal ошибку и не оставляет невосстановимую A2A Task в `working`.
- Перед каждой физической provider attempt runtime MUST одной транзакцией
  списать общий model budget и сохранить local attempt counter/dispatch marker.
  Для заранее оплаченного finalizer та же транзакция снимает reserve marker без
  второго списания. Crash после marker не теряет usage и не разрешает бесплатный
  повтор; неизвестный finalizer завершается детерминированным partial fallback.
- Перед обработкой каждого model-issued tool request runtime MUST одной
  workflow-транзакцией списать общий tool budget и сохранить local usage,
  выбранный request и pre-dispatch marker. Crash между charge и dispatch не
  списывает request повторно; rollback не оставляет расхождение ledger и
  checkpoint.
- Recovery воспроизводит state из event log, сверяет checkpoint и возвращает run в последнее доказуемо безопасное состояние.
- Pending input восстанавливается с теми же IDs и revision.
- Model streaming MAY быть перезапущен только если незавершённый ответ не породил side effect; частичный пользовательский текст помечается superseded.
- Любая неопределённость вокруг внешней мутации требует reconciliation или `SIDE_EFFECT_UNKNOWN`, а не оптимистичного продолжения.
- Доказанная runtime-ом ошибка schema/contract validation либо запуска process до dispatch и завершённые `failed`/`timed_out` tool outcomes записываются в context как tool result и возвращают workflow в `RUNNING`; модель получает следующий turn для исправления вызова или понятного ответа пользователю.
