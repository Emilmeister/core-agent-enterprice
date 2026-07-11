# Execution environment

## Инвариант изоляции

Команды агента, сабагентов, skill scripts и stdio MCP MUST NOT исполняться на машине или в namespace control plane. Tool Runtime обращается к ExecutionEnvironment adapter, который запускает работу в отдельной изолированной среде.

Модель deployment MAY использовать container, microVM, VM или удалённый sandbox service, если выполняет одинаковый security contract. Обычный host subprocess не соответствует спецификации.

## Граница control plane / execution plane

Control plane содержит orchestrator, policy, stores, model/MCP routing и telemetry control. Execution plane содержит пользовательский workspace, процессы и разрешённый network access.

Между ними передаются только typed requests/results, artifacts и policy decisions. Sandbox не получает control-plane filesystem, service credentials, container runtime socket или unrestricted store connection.

## Изоляция среды

ExecutionEnvironment MUST предоставлять:

- отдельные mount, process, PID, user, IPC и network boundaries либо эквивалент удалённой VM;
- непривилегированного пользователя без host capabilities;
- immutable base image и ephemeral writable layer;
- отдельные `/tmp`, home, process table и environment;
- CPU, memory, process, disk, output и wall-time limits;
- egress deny-by-default с domain/IP/port policy;
- отсутствие host root, Docker/container socket, SSH agent и cloud instance metadata;
- seccomp/syscall или эквивалентную policy, если платформа поддерживает;
- гарантированный teardown всего process tree.

Недоступность обязательной изоляции завершает действие с `EXECUTION_ENVIRONMENT_UNAVAILABLE`; fallback на host запрещён.

## Workspace

- Workspace создаётся из immutable artifact/snapshot, а не прямого unrestricted bind mount host project.
- Writable paths перечислены policy; остальные read-only или отсутствуют.
- Parent и child получают отдельные copy-on-write overlays по умолчанию.
- Результат изменений экспортируется как content-addressed snapshot или patch с base revision.
- Merge выполняется orchestrator-ом с conflict detection и audit.
- Durable task checkpoint хранит workspace snapshot reference, но не живой host path.

Shared writable volume допускается только для workflow, где concurrency policy и filesystem locking явно заданы; это не default для сабагентов.

## Secrets

- Secret material инжектируется непосредственно adapter-ом только в разрешённый process/call.
- Lifetime ограничен task/tool call; после завершения environment secret недоступен.
- Значение не входит в image, checkpoint, command arguments, telemetry или artifact.
- Child получает только secrets, явно разрешённые delegation contract и host policy.

## Network

- DNS и egress проходят policy gateway с audit.
- Redirect, resolved IP и повторное DNS resolution проверяются против policy для защиты от SSRF/rebinding.
- Inbound connections запрещены, кроме brokered port/preview capability с approval.
- stdio MCP запускается внутри environment; remote MCP вызывается через policy-aware egress proxy.
- A2A и OTel control traffic идут через control plane, а не доступны произвольному process.

## Lifecycle

1. Resolve immutable image и workspace snapshot.
2. Применить tenant/run/child policy и resource limits.
3. Создать environment и attested environment ID.
4. Выполнить tool/task с heartbeat и cancellation channel.
5. Собрать bounded outputs, patches и artifacts.
6. Завершить process tree и сеть.
7. Уничтожить writable layer либо сохранить encrypted checkpoint snapshot по policy.
8. Зафиксировать cleanup outcome и telemetry.

## Reuse

Environment MAY переиспользоваться между tool calls одного run для производительности, если:

- security context и workspace revision не изменились;
- нет unresolved side effect или leaked process;
- reset policy очищает временные credentials и unexpected listeners;
- reuse не пересекает tenants или parent/child boundary.

После high-risk call, policy change или failed cleanup среда уничтожается.

## Observability без утечки

Execution spans и metrics экспортируются через контролируемый collector. Raw stdout/stderr, commands, file contents и environment variables не являются span attributes по умолчанию. `environment_id`, image digest, resource class и exit status допускаются после cardinality review.
