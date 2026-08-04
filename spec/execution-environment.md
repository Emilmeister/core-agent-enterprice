# Локальные terminal sessions

## Deployment constraint

Целевой Core Agent запускается как один пользовательский container в managed-платформе Cloud.ru AI Agents. Runtime не имеет Kubernetes API, container runtime socket и возможности создавать вложенные containers, Pods или VM. Main agent, его сабагенты и разрешённые локальные процессы разделяют container OS и базовую файловую систему.

Внешний sandbox service не является обязательной частью продукта. Исполнение внутри этого container является осознанной trust-моделью: сабагентов создаёт основной агент, а локальные команды, skill scripts и stdio MCP считаются разрешёнными policy артефактами. Эта модель предоставляет lifecycle и workspace separation, но не является security boundary против намеренно враждебного process.

## TerminalSession

Main agent и каждый child-agent Task получают отдельную `TerminalSession`:

- stable session ID и owner agent/task ID;
- отдельный PTY для интерактивного процесса;
- отдельную process group;
- отдельные `cwd`, local workspace root и environment allowlist;
- независимые stdin/stdout/stderr streams и bounded output;
- собственные timeout, CPU/process/output budgets;
- lifecycle `created -> running -> exited|timed_out|canceled|failed -> closed`.

Сессия одного agent MUST NOT адресоваться terminal tools другого agent. Runtime проверяет owner ID на каждом `exec`, `write`, `read`, `signal` и `close`.

## Гарантии и не-гарантии

Runtime MUST обеспечивать:

- отдельные workspace directories для main и каждого child;
- отдельные PTY/process groups и независимую отмену;
- запуск только через typed `argv` по умолчанию; shell string требует отдельной policy;
- явный `cwd`, очищенный environment и secret allowlist;
- wall-time, output и доступные OS resource limits;
- завершение всей известной process group при cancel/timeout/close;
- отсутствие Docker/container socket, Kubernetes credentials и platform control-plane credentials в command environment;
- audit и OTel lifecycle без raw command/output по умолчанию.

Runtime не утверждает, что локальные processes имеют отдельные mount/PID/user/network namespaces. Process в общей container OS технически может видеть доступные тому же Unix user файлы или `/proc`. Это принятый риск целевого deployment, а не скрытая sandbox guarantee.

Если PTY или process-group primitives недоступны, terminal capability фильтруется до model context либо обязательный terminal завершается `EXECUTION_ENVIRONMENT_UNAVAILABLE`.

## Workspace и S3

S3-backed mount используется как durable storage для immutable snapshots, checkpoints, event segments и artifacts. Он не является active workspace для Git, package managers, compilers, SQLite или процессов, которым нужна полная POSIX semantics.

Активный workspace размещается на локальной ephemeral filesystem container-а:

```text
/tmp/core-agent/runs/<run-id>/agents/<agent-id>/workspace
```

Lifecycle workspace:

1. скопировать и проверить immutable base snapshot из S3 mount;
2. создать отдельную local directory main/child;
3. выполнять процессы только с назначенным `cwd`;
4. сформировать bounded patch и content-addressed artifacts;
5. записать новый immutable S3 prefix;
6. последним опубликовать revision/commit manifest;
7. удалить local workspace после terminal state или retention timeout.

Parent и child не должны одновременно изменять одну local directory. Каждый получает отдельную копию одного base revision; parent применяет child patch с conflict detection. Shared directory допускается только явной policy для доверенного workflow.

Если несколько replicas используют общий S3 mount, file lock на mount не считается distributed lease. До появления conditional object writes или внешнего lease store stateful run MUST иметь одного active owner.

## Secrets

- Container-wide environment не должен содержать секреты, доступные всем локальным processes.
- Tool call передаёт только разрешённые `secret_refs`.
- Runtime materializes значение непосредственно перед запуском и добавляет только в environment этого process.
- Значение не входит в command arguments, checkpoint, telemetry или artifact.
- После завершения process group уничтожается; долговременная TerminalSession не сохраняет secret в своём базовом environment.
- Child получает только secret refs, явно разрешённые delegation contract и platform policy.

Локальная process isolation не гарантирует защиту секрета от намеренно враждебного process того же Unix user. Поэтому secret-bearing terminal call всегда проходит отдельный risk decision; policy MAY запретить его полностью.

## Network

Локальные процессы наследуют network boundary container-а. Runtime применяет tool/MCP allowlists и MAY использовать application egress proxy, но не рекламирует отдельный network namespace для каждой TerminalSession.

- remote MCP проходит local policy до соединения и каждого call;
- redirects/resolved IP проверяются, когда соединение проходит через управляемый egress adapter;
- stdio MCP запускается как owned local process в TerminalSession;
- inbound listener разрешается только явной tool policy и закрывается вместе с session;
- A2A и OTel credentials не передаются в child process environment.

## Lifecycle

1. Проверить effective terminal capability, owner и budgets.
2. Создать local workspace и TerminalSession.
3. Запустить process с новым PTY/process group.
4. Неблокирующе читать bounded output и принимать input/signal.
5. На safe boundaries публиковать status/artifacts/notifications.
6. При cancel/timeout послать graceful signal, затем завершить process group.
7. Собрать patch/artifacts и опубликовать durable manifest в S3.
8. Закрыть PTY, удалить local workspace по policy и записать cleanup outcome.

## Python CodeAct process

`core.python.exec` переиспользует owned local workspace и process-group lifecycle TerminalSession, но запускает interpreter с очищенным environment и отдельным локальным IPC channel к parent runtime. IPC выдаёт только список разрешённых имён и `tools.call`; MCP credentials, database handles, ToolRuntime objects и operator authority в child process не materialize-ятся.

Этот внутренний process backend работает и в `without_terminal`: model-visible `core.terminal.exec` и `core.task.start` при этом отсутствуют. Python остаётся доверенным локальным кодом и может использовать стандартные OS/process APIs; разделение runtime modes не добавляет OS security boundary.

Parent является единственным tool broker: проверяет каждый canonical name/arguments по неизменному EffectiveConfig run-а, списывает общий budget и исполняет вызов через существующий built-in/MCP dispatch. IPC имеет single-run capability token, owner-only local endpoint, bounded JSON frames и закрывается вместе с Python process. Эта схема остаётся process separation, а не OS security boundary.

Пока Python continuation нельзя надёжно checkpoint/resume, capability не поддерживает background start. Timeout или cancel завершают Python process group; уже начатый вложенный side effect следует обычным downstream idempotency/reconciliation guarantees и не повторяется автоматически.

## Параллельность и reuse

TerminalSession принадлежит ровно одному main/child agent, но несколько sessions MAY работать параллельно в одном container в пределах общего semaphore и aggregate resource budget.

Сессия MAY жить между несколькими tool calls одного agent для REPL, debugger или server process. Обычный `core.terminal.exec` SHOULD запускать отдельный process в той же owned session/workspace; persistent interactive state используется только явно.

После timeout, failed cleanup или terminal agent state session закрывается и не переиспользуется другим agent.

## Observability без утечки

Spans отражают session ID, owner kind, process state, duration, exit status, timeout/cancel и bounded byte counts. Raw command, stdin/stdout/stderr, file content, environment и S3 paths выключены по умолчанию. Background terminal process получает новый execution trace со Span Link на task submission.
