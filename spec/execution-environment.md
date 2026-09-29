# Локальные terminal sessions

## Deployment constraint

Целевой deployment — один Kubernetes Pod для всех чатов компании. Python, terminal и фоновые команды MUST запускаться через Bubblewrap, с собственными mount/PID/IPC/network namespaces, ограничением syscalls и профилем доверенного runtime. Отдельные pods, privileged mode, host runtime socket и широкие node privileges не требуются и не разрешают обходить этот профиль.

Совместимость user namespaces/seccomp/AppArmor/SELinux с целевым кластером требует реальной проверки до release. Установка Bubblewrap сама по себе не доказывает изоляцию. Если обязательный sandbox или сетевой контроль отсутствует, команда отклоняется до запуска с `EXECUTION_ENVIRONMENT_UNAVAILABLE`; fallback без ограничений запрещён.

В sandbox доступны только чат как `/workspace`, собственные временные файлы и необходимые read-only программы/библиотеки. Корень соседних чатов, server data, platform credentials и Kubernetes service-account token не монтируются. `/proc` показывает только изолированные процессы; лишние inherited descriptors закрыты. CPU, memory/process limits ограничиваются отдельно; общее ядро остаётся границей доверия.

## TerminalSession

Main agent и каждый child-agent Task получают отдельную `TerminalSession`:

- stable session ID и owner agent/task ID;
- отдельный PTY для интерактивного процесса;
- отдельную process group;
- явные `cwd` в пределах workspace своего чата или выделенной scratch-копии и собственный environment allowlist;
- независимые stdin/stdout/stderr streams и bounded output;
- собственные timeout, CPU/process/output budgets;
- lifecycle `created -> running -> exited|timed_out|canceled|failed -> closed`.

Сессия одного agent MUST NOT адресоваться terminal tools другого agent. Runtime проверяет owner ID на каждом `exec`, `write`, `read`, `signal` и `close`.

## Гарантии и не-гарантии

Runtime MUST обеспечивать:

- постоянный workspace чата для его последовательных root Tasks; отдельные owned process/session для child и optional scratch copies там, где нужна изоляция изменений с последующим merge;
- отдельные PTY/process groups и независимую отмену;
- запуск только через typed `argv` по умолчанию; shell string требует отдельной policy;
- явный `cwd`, очищенный environment и secret allowlist;
- wall-time, output и доступные OS resource limits;
- завершение всей известной process group при cancel/timeout/close;
- отсутствие Docker/container socket, Kubernetes credentials и platform control-plane credentials в command environment;
- audit и OTel lifecycle без raw command/output по умолчанию.

Runtime MUST обеспечить проверенную mount/PID/IPC/network isolation для недоверенных процессов. Изоляция проверяется для symlink, `/proc`, собственного/чужого broker socket и subprocess; владение Unix user само по себе не является защитой.

Если PTY или process-group primitives недоступны, terminal capability фильтруется до model context либо обязательный terminal завершается `EXECUTION_ENVIRONMENT_UNAVAILABLE`.

## Постоянный workspace и snapshots

### FILE-01. Workspace

- Согласованная среда развёртывания — один Kubernetes Pod; все чаты
  обрабатываются в этом pod. Отдельные pod на чат не предполагаются.
- Python, terminal и фоновые команды изолируются через Bubblewrap по [профилю Bubblewrap](execution-environment.md).
- При создании чата ему назначается отдельная постоянная папка.
- Задачи этого чата работают с одной папкой, в том числе повторные cron-запуски.
- Родительская папка задаётся переменной окружения.
- Файлы сохраняются при restart сервиса.
- Внешний агент и выполняемый по его запросу код не имеют доступа к соседним
  чатам и их файлам.

Текущее `LOCAL_WORKSPACE_ROOT` имеет семантику ephemeral workspace для run.
Для постоянных папок чатов используется отдельный `CHAT_WORKSPACE_ROOT`,
с сохранением существующих временных рабочих областей там, где они нужны.
Имя новой переменной — техническая конкретизация согласованной настройки
через env. Bubblewrap предоставляет процессу папку текущего чата как
`/workspace`; смысл существующей переменной нельзя менять молча.

`DURABLE_STORAGE_ROOT` сохраняет immutable snapshots, manifests и transport blobs и MAY использовать S3-backed mount. `CHAT_WORKSPACE_ROOT` требует постоянный том с рабочей POSIX semantics для Git, package managers, compilers и файлов чата. `LOCAL_WORKSPACE_ROOT` остаётся ephemeral scratch; его прежняя семантика не меняется.

Каждый чат имеет одну постоянную папку, повторные задачи и cron используют её. Отдельные child scratch copies MAY материализоваться из проверенного base snapshot и возвращать patch с conflict detection. Parent/child не меняют пересекающиеся targets параллельно без явной координации. Terminal completion удаляет временные окружения, но сохраняет папку чата. Snapshot restore учитывает текущую revision и tombstones ручной очистки; удалённые файлы не воскресают.

Shared storage lock не заменяет PostgreSQL lease; у каждого stateful run один fenced owner.

## Secrets

- Container-wide environment не должен содержать секреты, доступные всем локальным processes.
- Tool call передаёт только разрешённые `secret_refs`.
- Runtime materializes значение непосредственно перед запуском и добавляет только в environment этого process.
- Значение не входит в command arguments, checkpoint, telemetry или artifact.
- После завершения process group уничтожается; долговременная TerminalSession не сохраняет secret в своём базовом environment.
- Child получает только secret refs, явно разрешённые delegation contract и platform policy.

Task-specific secret допускается только отдельной policy и не ослабляет запрет доступа к platform credentials, соседним процессам и чужому workspace.

## Network

### NET-01. Публичный интернет и закрытая внутренняя сеть

Python, terminal и их дочерние процессы могут обращаться в публичный интернет,
в том числе через `requests`, `curl` и менеджеры пакетов. Отдельный перечень
разрешённых публичных доменов не требуется. Установка пакетов сохраняет общую
tool policy/HITL и записывает файлы только в разрешённую область чата.

Трафик sandbox проходит через контролируемый выход из его network namespace.
Нельзя просто разделить с ним сеть серверного процесса или положиться только
на переменные `HTTP_PROXY`/`HTTPS_PROXY`: произвольный код не должен обходить
границу прямым socket-соединением. Профиль запуска и сетевые правила нельзя
изменить из sandbox.

Недоступны приватные, link-local и иные непубличные назначения, адреса соседних
окружений, Pod/Service/node сети кластера, внутренние API и metadata endpoints.
Защищённые адреса конкретного развёртывания учитываются явно, в том числе если
они используют публичный диапазон. Нельзя достичь localhost серверного pod
через адрес шлюза или иной маршрут; loopback внутри собственного sandbox
остаётся локальным этому окружению.

Ограничение проверяет фактический адрес назначения каждого соединения, включая
переходы по redirect и изменение DNS-ответа. Оно распространяется на IPv4 и
IPv6 и не обходится альтернативным представлением адреса или протоколом.
DNS для публичных ресурсов должен работать через контролируемый resolver;
это не открывает произвольный доступ к внутренней сети.

Защищённый сервер сохраняет необходимый ему доступ к PostgreSQL, Keycloak,
MCP и доверенным A2A endpoints. Доступ sandbox к такому инструменту возможен
через собственный broker после проверки policy/HITL; сетевые credentials
сервера процессу не выдаются.

Конкретный egress gateway и поддерживаемые протоколы MUST быть проверены до release. HTTP-only proxy нельзя выдавать за универсальный прямой доступ; конкретный userspace networking компонент этим контрактом не выбран.

Обычная Kubernetes NetworkPolicy применяется к pod и может быть дополнительной
границей, но не заменяет разные права сервера и чатов внутри одного pod.
При отказе сетевого контроля нельзя переключаться на неограниченную сеть pod.

## Lifecycle

1. Проверить effective terminal capability, owner и budgets.
2. Открыть постоянный workspace чата и создать owned TerminalSession; при необходимости подготовить отдельную scratch-копию.
3. Запустить process с новым PTY/process group.
4. Неблокирующе читать bounded output и принимать input/signal.
5. На safe boundaries публиковать status/artifacts/notifications.
6. При cancel/timeout послать graceful signal, затем завершить process group.
7. Сохранить committed файлы чата и при необходимости snapshot/transport results.
8. Закрыть PTY, удалить только ephemeral scratch по policy и записать cleanup outcome.

## Python CodeAct process

`core_python_exec` переиспользует owned local workspace и process-group lifecycle TerminalSession и общается с parent runtime отдельным локальным IPC channel. IPC выдаёт только список разрешённых имён и `tools.call`; MCP credentials, database handles, ToolRuntime objects и operator authority в child process не materialize-ятся. Граница проходит по IPC, а не по module search path.

Интерпретатор MUST быть тем же, который получает команда в terminal workspace. Разные интерпретаторы у двух тулов означают, что установленный из терминала пакет не импортируется в Python, причём молча: `pip install` завершается успехом, а следующий `import` — `ModuleNotFoundError`, и модель не может связать одно с другим. Собственный virtualenv агента для этого не годится: в нём нет ни pip, ни права на запись. Поэтому user site-packages и `PYTHONPATH` MUST быть видны, а рабочий каталог MUST NOT попадать в `sys.path` автоматически: файл, случайно названный именем модуля stdlib, иначе ломает сам runner.

Этот внутренний process backend работает и в `without_terminal`: model-visible `core_terminal_exec` и `core_task_start` при этом отсутствуют. Python может использовать OS/process APIs, но все его процессы остаются в обязательной Bubblewrap/egress границе; выбор runtime mode не отключает sandbox.

Граница кадра IPC MUST быть bounded и вмещать допустимые tool arguments. Большие файлы передаются проверенными transport/workspace references, без dedicated artifact tools.

Parent является единственным tool broker: проверяет canonical name/arguments по admission ceiling, актуальной owner policy, schema и budgets и использует общий dispatch/HITL. В sandbox доступен только Unix socket собственного вызова с single-run capability token; chat/tenant определяется сервером, а не аргументом процесса. Endpoint и bounded JSON frames закрываются с процессом; socket не открывает серверные credentials или чужие tools.

Пока Python continuation нельзя надёжно checkpoint/resume, capability не поддерживает background start. Timeout или cancel завершают Python process group; уже начатый вложенный side effect следует обычным downstream idempotency/reconciliation guarantees и не повторяется автоматически.

## Параллельность и reuse

TerminalSession принадлежит ровно одному main/child agent, но несколько sessions MAY работать параллельно в одном container в пределах общего semaphore и aggregate resource budget.

Сессия MAY жить между несколькими tool calls одного agent для REPL, debugger или server process. Обычный `core_terminal_exec` SHOULD запускать отдельный process в той же owned session/workspace; persistent interactive state используется только явно.

После timeout, failed cleanup или terminal agent state session закрывается и не переиспользуется другим agent.

## Observability без утечки

Spans отражают session ID, owner kind, process state, duration, exit status, timeout/cancel и bounded byte counts. Raw command, stdin/stdout/stderr, file content, environment и S3 paths выключены по умолчанию. Background terminal process получает новый execution trace со Span Link на task submission.
