# Runtime и reasoning

## Состояния запуска

Полная state machine определена в [Архитектуре](architecture.md). Runtime MUST поддерживать durable ожидание approval/input, pause/resume и recovery, а не удерживать worker или model connection открытыми.

Переход в терминальное состояние необратим.

## Инициализация

До первого вызова модели ядро MUST:

1. провалидировать RunRequest;
2. создать `run_id`, event stream и начальный checkpoint;
3. разрешить skills и MCP descriptors через host policy;
4. создать lock snapshot skills, MCP capabilities и доступных tools;
5. построить системные инструкции;
6. загрузить session working state и релевантную memory;
7. выбрать primary model route и проверить capabilities;
8. вычислить доступный контекстный бюджет;
9. испустить `run.started`.

Если шаг не выполнен, модель и инструменты MUST NOT вызываться.

## Состав системных инструкций

Ядро формирует инструкции в следующем порядке приоритета:

1. неизменяемые safety-инварианты платформы;
2. DeploymentConfig и host policy;
3. протокол Core Agent;
4. исходный пользовательский prompt;
5. инструкции активированных skills;
6. данные, полученные от tools.

Нижний уровень MUST NOT отменять верхний. Tool output считается недоверенными данными, даже если содержит текст, похожий на инструкции.

Если prompt и skill противоречат друг другу без нарушения уровней 1–3, явный prompt имеет приоритет. Если безопасное разрешение неоднозначно, агент запрашивает уточнение через итоговый ответ, не угадывает.

## Agent loop

В состоянии `RUNNING` ядро повторяет:

1. оценивает заполнение контекста и при необходимости выполняет compaction;
2. вызывает модель с активным контекстом и доступными tool schemas;
3. стримит пользовательский текст без скрытого reasoning;
4. если модель завершила ответ — завершает запуск;
5. если модель запросила tool — валидирует имя и arguments;
6. оценивает риск и при необходимости durable-переходит в `WAITING_APPROVAL`;
7. исполняет разрешённый tool, нормализует result и добавляет его в контекст;
8. продолжает цикл.

Последовательность является базовой семантикой. Runtime MAY построить dependency graph и параллельно исполнить доказуемо независимые read-only calls или изолированные child runs. Каждый call всё равно получает отдельные policy decision, lifecycle и audit. Порядок слияния результатов должен быть стабильным.

## Reasoning

- Ядро MUST использовать reasoning-capable режим модели, если он поддерживается выбранным provider.
- Уровень reasoning и model route выбираются внутренней политикой на основе сложности, latency target, data classification, capabilities и бюджета; клиент не управляет ими через RunRequest.
- Raw chain-of-thought, reasoning tokens и скрытые scratchpads MUST NOT попадать в события, логи или tool arguments.
- Ядро MAY отдавать краткое резюме намерения или основания действия, сформулированное для пользователя.
- Отсутствие обязательной capability MUST быть известно до запуска и либо компенсировано разрешённым fallback, либо завершаться `MODEL_CAPABILITY_MISSING`.

## Model routing и fallback

Route описывается требованиями, а не именем модели: context window, modalities, tool calling, reasoning, structured output, region, data policy, latency и price ceiling.

- Provider adapter MUST объявлять capabilities и effective limits.
- Fallback MUST сохранять system/user semantics и tool call state.
- Переход на модель с меньшим окном требует compaction до вызова.
- Provider не может получить данные, запрещённые tenant routing policy.
- После начала mutating tool protocol fallback не должен повторять уже принятый tool call.
- Смена route отражается событием с безопасной причиной, provider/model MAY быть скрыты host policy.

## Human input

Если агенту не хватает факта или выбора, который нельзя безопасно вывести, он создаёт `input.required` со schema ожидаемого ответа и durable-переходит в `WAITING_INPUT`. Это отличается от approval: input даёт данные или решение задачи, approval разрешает уже сформулированный side effect.

Runtime SHOULD объединять связанные вопросы в один запрос и не спрашивать то, что можно безопасно обнаружить доступными read-only tools.

## Делегирование

Runtime MAY вызвать внутренний `core.delegate`. До запуска child run orchestrator задаёт:

- узкую цель и success criteria;
- разрешённые tools/MCP/skills;
- data visibility;
- token/cost/time budget;
- максимальные depth и fan-out;
- формат structured result.

Parent остаётся ответственным за проверку результата. Child summary считается недоверенным результатом tool, а не новым системным правилом.

## Завершение

Агент завершает запуск, когда:

- задача выполнена и сформирован итоговый ответ;
- безопасное продолжение требует новых пользовательских данных;
- пользователь отклонил критически необходимое действие;
- ожидаемый human input недоступен после policy timeout;
- достигнут hard budget;
- произошла невосстановимая ошибка;
- получена команда отмены.

Отклонение approval не является автоматической ошибкой: tool result с отказом возвращается модели, чтобы она могла выбрать безопасную альтернативу или объяснить блокировку.

## Budgets и защита от зацикливания

DeploymentConfig MUST задавать hard limits минимум для:

- числа model turns;
- числа tool calls;
- wall-clock времени;
- стоимости или токенов, если provider даёт такую метрику;
- размера одного tool result и суммарного artifact storage.
- depth/fan-out и суммарного бюджета child runs;
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
