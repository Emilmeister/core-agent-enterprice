# Runtime и reasoning

## Состояния запуска

Полная state machine определена в [Архитектуре](architecture.md). Runtime MUST поддерживать durable ожидание approval/input, pause/resume и recovery, а не удерживать worker или model connection открытыми.

Переход в терминальное состояние необратим.

## Инициализация

До первого вызова модели ядро MUST:

1. провалидировать A2A Message/Core extension;
2. вычислить immutable EffectiveConfig;
3. создать `run_id`, event stream и начальный checkpoint;
4. разрешить skills и MCP descriptors через effective policy;
5. создать lock snapshot skills, MCP capabilities и отфильтрованных tools;
6. построить system/kernel/capability instructions;
7. загрузить session working state и, если разрешено, вызвать retrieval MCP включая Memory Service;
8. выбрать primary model route и проверить capabilities;
9. вычислить доступный контекстный бюджет;
10. перевести A2A Task в `working` и испустить внутреннее `task.started`.

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

Нижний уровень MUST NOT отменять верхний. Memory protocol, обязательные tools, delegation rules, task lifecycle, approvals, TerminalSession ownership/lifecycle и observability принадлежат KernelInstructions и одновременно enforced runtime-ом. AgentProfilePrompt не может их заменить. Полный contract описан в [Kernel instructions](kernel-instructions.md).

Если prompt и skill противоречат друг другу без нарушения уровней 1–3, явный prompt имеет приоритет. Если безопасное разрешение неоднозначно, агент запрашивает уточнение через итоговый ответ, не угадывает.

## Agent loop

В состоянии `RUNNING` ядро повторяет:

1. принимает durable task notifications и новые inbound Messages на safe boundary;
2. добавляет каждый принятый Message отдельным user turn с provenance и committed sequence;
3. оценивает заполнение контекста и при необходимости выполняет compaction;
4. вызывает модель с активным контекстом и доступными tool schemas;
5. стримит пользовательский текст без скрытого reasoning;
6. если модель завершила ответ — атомарно проверяет inbound inbox и завершает запуск только при отсутствии более раннего unread Message;
7. если модель запросила tool — валидирует имя и arguments;
8. оценивает риск, замораживает exact action и при необходимости durable-переходит в `WAITING_LOCAL_APPROVAL`;
9. исполняет разрешённый tool, нормализует result и добавляет его в контекст;
10. продолжает цикл, другую независимую работу или passive wait.

Последовательность является базовой семантикой. Runtime MAY построить dependency graph и параллельно исполнить доказуемо независимые read-only calls или изолированные child Tasks. Каждый call всё равно получает отдельные policy decision, lifecycle и audit. Порядок слияния результатов должен быть стабильным.

## Live steering

Новый A2A Message не является interrupt текущей операции. Adapter durable-фиксирует его в inbox существующего non-terminal run; orchestrator забирает committed Messages перед следующим model turn. Если Message приходит между последним model response и terminal commit, completion gate обязан либо включить его в следующий turn, либо проиграть race уже committed terminal state и отклонить SendMessage.

Каждый accepted Message сохраняет исходные `messageId`, task/context IDs, authenticated caller provenance и monotonic inbox sequence, но поступает модели как недоверенный user-role input. Несколько Messages не склеиваются в один prompt и не подменяют system/kernel instructions. Unconsumed Messages pinned при compaction и recovery. Follow-up не сбрасывает и не увеличивает hard budgets.

Message возобновляет `WAITING_INPUT` или `WAITING_TASK` run. В `PAUSED`, `WAITING_AUTH` и `WAITING_LOCAL_APPROVAL` он остаётся queued до разрешённого resume, потому что remote text не может снять host pause, заменить auth flow или изменить frozen action/authority boundary.

## Reasoning

- Ядро MUST использовать reasoning-capable режим модели, если он поддерживается выбранным provider.
- Уровень reasoning и model route выбираются внутренней политикой на основе сложности, latency target, data classification, capabilities и бюджета; клиент не управляет ими через RunRequest.
- Raw chain-of-thought, reasoning tokens и скрытые scratchpads MUST NOT попадать в события, логи или tool arguments.
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

## Human input

Если агенту не хватает факта или выбора, который нельзя безопасно вывести, он создаёт `input.required` со schema ожидаемого ответа и durable-переходит в `WAITING_INPUT`. Это отличается от approval: input даёт данные или решение задачи, approval разрешает уже сформулированный side effect.

Runtime SHOULD объединять связанные вопросы в один запрос и не спрашивать то, что можно безопасно обнаружить доступными read-only tools.

## Фоновые задачи и делегирование

Long-running tool, indexing job и сабагент запускаются как неблокирующие Tasks. Parent получает handle, продолжает другую работу или durable-переходит в `WAITING_TASK`. Completion notification возобновляет loop и добавляется в контекст на safe boundary. Delegation MUST передавать явные allowlists tools/MCP/skills; детали определены в [Фоновых задачах и делегировании](tasks-and-delegation.md).

## Завершение

Агент завершает запуск, когда:

- задача выполнена и сформирован итоговый ответ;
- безопасное продолжение требует новых пользовательских данных;
- local operator отклонил критически необходимое protected action;
- ожидаемый human input недоступен после policy timeout;
- достигнут hard budget;
- произошла невосстановимая ошибка;
- получена команда отмены.

Наличие pending background task не требует держать run активно вычисляющимся. Agent MAY ничего не делать и ждать. Terminal completion допускается только если pending tasks отменены, detached по policy или явно не нужны результату.

Отклонение approval не является автоматической ошибкой: tool result с отказом возвращается модели, чтобы она могла выбрать безопасную альтернативу или объяснить блокировку.

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

При достижении 90% любого hard limit ядро SHOULD дать модели сигнал завершить задачу кратчайшим безопасным способом. При 100% запуск MUST остановиться с `BUDGET_EXCEEDED`; бесконечное автоматическое увеличение запрещено.

## Повторные попытки

- Read-only и идемпотентные provider/MCP операции MAY повторяться при временной ошибке с ограниченным exponential backoff.
- Мутирующий tool call MUST NOT повторяться автоматически, если нет достоверного idempotency key или подтверждения, что действие не началось.
- Каждая попытка сохраняется в аудите под одним логическим `tool_call_id` и отдельным `attempt`.

## Pause, recovery и lease

- `pause` запрещает новые model/tools после ближайшей безопасной границы и создаёт checkpoint.
- Worker MUST регулярно обновлять lease; только владелец актуального lease изменяет run.
- Recovery воспроизводит state из event log, сверяет checkpoint и возвращает run в последнее доказуемо безопасное состояние.
- Pending approval/input восстанавливаются с теми же IDs и revision.
- Model streaming MAY быть перезапущен только если незавершённый ответ не породил side effect; частичный пользовательский текст помечается superseded.
- Любая неопределённость вокруг внешней мутации требует reconciliation или `SIDE_EFFECT_UNKNOWN`, а не оптимистичного продолжения.
- Доказанная runtime-ом ошибка schema/contract validation либо запуска process до dispatch и завершённые `failed`/`timed_out` tool outcomes записываются в context как tool result и возвращают workflow в `RUNNING`; модель получает следующий turn для исправления вызова или понятного ответа пользователю.
