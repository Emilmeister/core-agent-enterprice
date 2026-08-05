# Безопасность и надёжность

## Trust boundaries

Доверенными являются только safety-инварианты ядра и валидированные PlatformConfig/AgentConfig. Следующие источники MUST считаться недоверенными:

- prompt;
- AgentProfilePrompt;
- memory content, metadata и derived graph results;
- skill instructions, scripts и resources;
- MCP metadata, tool schemas и outputs;
- A2A peer Messages, Agent Cards и Artifacts;
- terminal output;
- содержимое файлов workspace;
- текст, полученный из сети.

Недоверенный текст не может менять policy или получать секреты только через prompt injection.

## Local process trust boundary

Terminal, skill scripts, stdio MCP и child-agent commands исполняются в одном managed container через owned [TerminalSessions](execution-environment.md). Main и child имеют отдельные PTY, process groups, environment allowlists и workspace directories, но общий container OS не является security boundary против намеренно враждебного process. Этот риск принят продуктовой моделью; policy запрещает address чужих sessions и не рекламирует mount/PID/network isolation.

## Секреты

- Конфигурация содержит только ссылки на секреты; входящий Message не переносит их вовсе.
- Secret resolver работает после policy check и непосредственно перед использованием.
- Значения секретов MUST NOT попадать в model context, events, logs, errors или artifacts.
- Output проходит redaction известных значений и распространённых credential patterns.
- Модель получает только факт доступности секрета, если ей не требуется само значение для tool schema; предпочтительно secret injection выполняет adapter.

## Filesystem

- Все пути нормализуются и проверяются после разрешения symlinks.
- Проверка path traversal выполняется до чтения и до записи.
- `apply_patch` должен быть атомарным на уровне одного patch: либо применены все hunks, либо ни одного.
- Ядро MUST различать изменения этого запуска и существующие пользовательские изменения; оно MUST NOT откатывать последние без явного запроса.

## Процессы

- Каждый процесс принадлежит одному `run_id`, agent ID и TerminalSession.
- Output читается без неограниченного накопления в памяти.
- Тайм-аут завершает process tree, а не только родительский PID, если это поддерживается ОС.
- Интерактивный stdin адресуется непрозрачным session ID.
- После терминального состояния не должно оставаться процессов, кроме явно зафиксированного сбоя cleanup.

## Идемпотентность и side effects

- Ядро сохраняет tool call intent до исполнения.
- Для внешних мутаций adapter SHOULD передавать idempotency key из `tool_call_id`.
- Неопределённый исход мутирующего call MUST возвращаться как `SIDE_EFFECT_UNKNOWN`, а не автоматически повторяться.
- Итоговый ответ обязан сообщить о возможном частичном эффекте.

## Ошибки

Ошибка имеет стабильный `code`, безопасный `message`, `retryable` и внутренний correlation ID. Минимальный набор кодов:

- `INVALID_REQUEST`;
- `MODEL_UNAVAILABLE`, `MODEL_CAPABILITY_MISSING`;
- `MCP_CONNECTION_FAILED`, `MCP_PROTOCOL_ERROR`;
- `SKILL_INVALID`, `SKILL_RESOURCE_MISSING`, `SKILL_RESOURCE_INVALID`,
  `SKILL_ACTIVATION_BOUNDARY`;
- `TOOL_NAME_COLLISION`, `TOOL_ARGUMENT_INVALID`, `TOOL_EXECUTION_FAILED`;
- `POLICY_DENIED`;
- `EXECUTION_ENVIRONMENT_UNAVAILABLE`, `EXECUTION_ENVIRONMENT_COMPROMISED`;
- `BUDGET_EXCEEDED`, `CONTEXT_UNRECOVERABLE`;
- `SIDE_EFFECT_UNKNOWN`, `INTERNAL_ERROR`;
- `SESSION_CONFLICT`, `LEASE_LOST`, `CHECKPOINT_INVALID`;
- `CONFIG_INVALID`, `CONFIG_CONFLICT`, `CAPABILITY_DISABLED`, `REQUIRED_CAPABILITY_MISSING`, `TOOL_FILTER_EMPTY`;
- memory errors `MEMORY_FILE_TOO_LARGE`, `MEMORY_CONFLICT`, `MEMORY_INVALID`;
- `SKILL_INTEGRITY_FAILED`, `EXTENSION_REVOKED`;
- `MODEL_ROUTE_UNAVAILABLE`;
- `TASK_NOT_FOUND`, `TASK_NOT_CANCELABLE`, `TASK_NOTIFICATION_FAILED`;
- `A2A_VERSION_UNSUPPORTED`, `A2A_EXTENSION_REQUIRED`.

Stack traces, raw provider errors и секретные arguments MUST NOT попадать в публичный message. Они MAY сохраняться в защищённом operator log по correlation ID.

## Crash recovery

Durable continuation является свойством целевого runtime. Ядро MUST:

- дописывать audit events до публикации внешнему клиенту либо использовать атомарный эквивалент;
- восстанавливать незавершённые runs из event log и последнего проверенного checkpoint;
- сохранять сведения о начатых внешних side effects;
- автоматически продолжать только доказуемо безопасные или идемпотентные операции;
- переводить неоднозначную внешнюю мутацию в reconciliation, `WAITING_INPUT` или `ABORTED`;
- сохранять pending input с исходными IDs и scopes.

## Multi-tenancy

- Tenant identity устанавливается authenticated transport context, не prompt.
- Stores, caches, terminal sessions/workspaces, MCP connections и artifact URLs MUST быть tenant-scoped. Подсистема памяти отвечает за tenant isolation Markdown/BM25/vector/graph data.
- Cross-tenant identifiers возвращают not-found semantics, если раскрытие существования запрещено.
- Quotas применяются до выделения дорогого model/execution ресурса.
- Child runs всегда наследуют tenant и не могут сменить его через arguments.

## Supply chain

- Remote skill/MCP artifacts проверяются по integrity и trust policy.
- Dependency snapshot и signatures сохраняются в audit.
- Revocation feed MAY заблокировать новый run или приостановить активный.
- Provider и adapter versions фиксируются для воспроизводимости; security patches могут обновляться с явно записанной migration boundary.

## Policy versioning

Каждое решение policy хранит policy version и matched rules. Новая более строгая policy применяется к ещё не начатым actions активного run. Уже выданный grant повторно валидируется перед использованием. Более мягкая policy не ретроактивно одобряет ожидающее действие без нового evaluation.

## Data retention

Срок хранения транскриптов, checkpoints и artifacts задаёт PlatformConfig. Memory retention/delete выполняет подсистема памяти; Core обязан удалить свои cached excerpts и transcript-derived copies в той же публикации. Разрешены только агрегированные метрики и обязательные compliance tombstones.
