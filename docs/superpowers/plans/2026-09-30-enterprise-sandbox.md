# Enterprise sandbox, egress and process lifecycle — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax. Follow repository authorization for frozen spec/tests; this plan itself does not authorize their edits.

**Goal:** Запускать terminal, Python и фоновые команды в проверяемом Bubblewrap sandbox одного Pod, разрешая публичную сеть и закрывая внутренние назначения, с обязательным завершением всех принадлежащих запуску процессов.

**Architecture:** Один конкретный launcher под существующим `TerminalSessionManager`: Bubblewrap создаёт namespaces и ждёт закрытого launch gate, доверенный supervisor устанавливает nftables и поднимает slirp4netns, затем разрешает команду. Inner seccomp не даёт команде изменить namespaces, capabilities или сетевую границу. Workspace и Python broker передаются launcher-у готовыми доверенными путями; launcher не реализует admission или файловый service.

**Tech Stack:** Существующие Python 3.12, stdlib, unittest и uv; Linux Bubblewrap, slirp4netns, nftables, util-linux и libseccomp. Kubernetes user namespaces, OCI seccomp и конечные Pod resource limits. Нового Python dependency или отдельного sandbox daemon нет.

## Объём и подтверждённая исходная точка

Нормативные источники: [execution-environment](../../../spec/execution-environment.md), [security-and-reliability](../../../spec/security-and-reliability.md), [runtime](../../../spec/runtime.md), [enterprise design §12](../specs/2026-09-29-enterprise-agent-design.md). Соседний файловый этап параллельно фиксируется в `docs/superpowers/plans/2026-09-30-enterprise-files.md`. Не включать сюда attachment publication, quarantine decisions, UI, миграцию пользовательских blobs или удаление artifact tools.

2026-09-30 повторно прочитаны prototype `/tmp/core-agent-sandbox-network-probe.py`, Dockerfile и Pod manifest рядом с ним. Через **явный** `/tmp/core-agent-enterprise.kubeconfig` подтверждены `core-agent-sandbox-probe: Succeeded`, `public-https 200`, `CapEff: 0000000000000000`, `private-connect-error 111`, `sandbox-exit 0`. Среда: kind, Kubernetes `v1.37.0`, Linux `6.12.54-linuxkit`, arm64, containerd `2.3.4`.

Это только проверка создания namespaces, одного HTTPS запроса и одного отказа gateway. Текущий prototype принимает весь оставшийся IPv4, отбрасывает IPv6, не применяет inner seccomp и ресурсные пределы, не ограничивает helper environment и не проверяет полное дерево процессов. OCI profile из `/tmp/core-agent-oci-seccomp.json` рассчитан на arm64. Эти временные файлы нельзя просто скопировать и объявить production profile.

Разделить реализацию на два reviewable commits:

1. **Launcher:** новый изолированный launcher, image/Pod fixtures и реальные Linux tests; runtime ещё не подключён, capability не объявляется implemented.
2. **Integration:** единственный shared execution path, trusted workspace/broker binding, terminal/recovery cleanup и обычные CI gates. Только после него штатные agent tools могут использовать launcher.

Отсутствие целевого кластера не блокирует локальный первый commit, но не позволяет объявить target deployment совместимым. Оба commit выполняются после нужного разрешения менять frozen tests; это документированный план, не разрешение на их изменение.

Рабочее состояние 30 сентября: standalone `sandbox.py`/`sandbox_exec.py`, unit tests
и определения native Linux tests подготовлены; runtime ещё не подключён. Unit
проверки и Ruff прошли. Обязательный режим `CORE_AGENT_REQUIRE_SANDBOX_TESTS=1`
в текущей macOS среде завершается ошибкой native-Linux precondition, а обычный
suite явно пропускает Linux class; это не считается выполненным sandbox gate.
После смены прав среды Docker, Kubernetes и локальные listening sockets недоступны.
CI job и запуск native Pod остаются незавершёнными, commit не создан.

До release дополнить и реально выполнить: runtime-parent SIGKILL на каждой
стадии запуска, helper filesystem/FD confinement, установку пакетов/компиляцию,
полный набор доступных metadata/link-local/Pod/Service receivers. Проверить
эффективные cgroup limits при finite Pod ancestor и unlimited container leaf,
а также достаточность сверки firewall readback. Наличие тестового исходника не
закрывает эти пункты без выполнения на поддерживаемом Linux/ABI.

## Точные точки интеграции

| Файл | Изменение |
|---|---|
| `core_agent/sandbox.py` — новый | Конкретные `SandboxPolicy`, `SandboxProcess`, `SandboxLauncher`; запуск supervisor, namespace gates, nft/slirp lifecycle и native inner seccomp. В том же модуле закрытый CLI supervisor; без plugin/backend framework. |
| `core_agent/sandbox_exec.py` — новый | Маленький trusted stdlib-only trampoline внутри sandbox: hard rlimits, READY, fail-closed exec gate, закрытие служебных FD, затем `execve` target. Это отдельная security boundary, а не второй launcher. |
| `core_agent/execution.py` | `_LocalTerminalSession.start` использует launcher вместо прямого target `Popen`; `ProcessHandle` хранит owned sandbox handle; `_terminate`, `wait`, `destroy`, `TerminalSessionManager.close` закрывают весь sandbox. Убрать ложные process-only security flags. |
| `core_agent/python_exec.py` | `execute_python` передаёт текущий broker socket доверенным аргументом; runner видит только `/run/core-agent/broker.sock`, сохраняет token authentication и bounded frames. |
| `core_agent/tools.py` | `_execute` terminal остаётся общим entrypoint; не добавлять альтернативный subprocess path. Передавать уже проверенный run binding, а не путь модели. |
| `core_agent/runtime.py` | Scope binding при `_initialize_workflow`/`_restore_runtime`, `_task_start` и `_recover_background_tool`; отдельные owned sessions для children; bounded cleanup до terminal transition и при lease loss/shutdown. |
| `core_agent/app.py` | Создать один launcher/policy, startup preflight, передать shared manager; shutdown закрывает manager до databases. |
| `Dockerfile`, `.env.example`, `docker-compose.yml` | Native packages, immutable profiles, явная конфигурация; несовместимый Compose не получает unsandboxed fallback. |
| `deploy/security/oci-amd64.json`, `deploy/security/oci-arm64.json` — новые | OCI allowlists для supervisor/bwrap bootstrap, закреплённое происхождение и checksum. Это внешний профиль, не inner policy приложения. |
| `core_agent/sandbox-policy.json` — новый | Native syscall policy и versioned IPv4/IPv6 CIDR tables; включить в wheel/image как read-only package resource, входит в module data без дополнительного build hook. |
| `deploy/kubernetes/sandbox-pod.yaml`, `deploy/kubernetes/kind-sandbox.yaml` — новые | Проверяемый deployment fragment и локальная Linux CI topology, без обязательного Helm chart. |
| `tests/test_sandbox.py`, `tests/test_sandbox_linux.py` — новые | Малые policy/failure tests и реальные process/network adversarial tests; никаких mocked isolation success assertions. |
| `tests/test_local_terminal.py`, `tests/test_python_exec.py`, `tests/test_tasks_tools_execution.py`, `tests/test_runtime_observability.py`, `tests/test_postgres_persistence.py` | Существующие execution/lifecycle/recovery assertions дополняются фактическими sandbox outcomes; не удалять прежние guarantees ради Linux-only кода. |
| `.github/workflows/ci.yml`, `README.md`, `AGENTS.md` | Mandatory Linux sandbox job и честные deployment instructions; менять при реализации, не в этом planning change. |

`EnvironmentSpec.hardened()` сейчас всегда задаёт `isolation_level="process"`, `os_security_boundary=False`; строка `resource_limits` в capability set не означает действующие rlimits. `EgressPolicy.allow()` сейчас не вызывается процессами и не является firewall. Не сохранять его как конкурирующий механизм allowlist публичных доменов.

## Общий launch contract

Launcher принимает только runtime-owned значения; приведённая сигнатура — граница реализации, не дополнительный model tool:

```python
launcher.start(
    argv,
    workspace_path=trusted_workspace_path,
    readonly_input_path=trusted_attachments_path,
    cwd=workspace_relative_cwd,
    environment=validated_process_environment,
    stdin=slave_fd,
    stdout=slave_fd,
    stderr=slave_fd,
    broker_socket=trusted_socket_or_none,
)
```

`SandboxProcess` владеет supervisor, bwrap, namespace-init pidfd, slirp, gate/status/exit descriptors и временным launch directory. Его `stop(grace_seconds)` idempotent; возврат означает подтверждённое отсутствие target descendants и helpers либо явный cleanup failure. `wait(timeout)` возвращает target exit status, а не статус последнего cleanup helper.

Файловый этап отвечает за resolver `run → tenant + immutable chat owner + context → workspace_path`. Нельзя подставлять текущего actor вместо owner чата. Он же передаёт `readonly_input_path=workspace_path/attachments`: launcher накладывает read-only bind на `/workspace/attachments` поверх общего RW `/workspace`. Host ingress публикует batch directories через удержанный no-follow directory FD; process не может подменить mountpoint или менять принятый input. Для редактирования агент копирует input в writable tree. Пока resolver не подключён, standalone tests передают собственные временные workspace; runtime integration не придумывает persistent layout самостоятельно. Child/background наследуют **тот же chat binding**, отдельный session owner и собственные process handles. Scratch path допустим только как явно выбранная runtime область этого чата.

## Задача 1. Профиль и проверка окружения до запуска команды

**Files:** `core_agent/sandbox.py`, `deploy/security/*`, `.env.example`, `tests/test_sandbox.py`.

- [ ] Ввести immutable `SandboxPolicy` со следующими deployment settings. Все числа конечны и положительны; bool не считается int. Пустые/повреждённые CIDR, DNS и неподдерживаемая архитектура дают `CONFIG_INVALID` до запуска инструментов.

| Setting | Значение/семантика |
|---|---|
| `SANDBOX_DNS_SERVERS` | Обязательный список literal public resolver IP, например `1.1.1.1,2606:4700:4700::1111`; host/cluster resolv.conf не наследуется. |
| `SANDBOX_DENIED_CIDRS` | Обязательный deployment список Pod/Service/node и внутренних сервисов, включая защищённые публичные диапазоны. Объединяется с immutable special-purpose deny; не заменяет его. |
| `SANDBOX_START_TIMEOUT_SECONDS` | `10`: общий deadline bootstrap, nft readback и slirp readiness. |
| `SANDBOX_MAX_CONCURRENT_PROCESSES` | `4`: process-local semaphore для всей компании, включая background/child; bounded ожидание учитывается timeout вызова. |
| `SANDBOX_CPU_SECONDS` | `60`: hard CPU rlimit каждого target process; inherited descendants не получают право повышать его. |
| `SANDBOX_ADDRESS_SPACE_BYTES` | `2147483648`: hard `RLIMIT_AS`; это virtual address space, не aggregate RSS. |
| `SANDBOX_MAX_OPEN_FILES` | `256`: hard `RLIMIT_NOFILE`. |
| `SANDBOX_MAX_FILE_BYTES` | `104857600`: hard `RLIMIT_FSIZE`; независим от transport attachment limit. |

Public egress — единственный фиксированный versioned профиль, без отдельного
переключателя с одним значением. IPv4 и IPv6 фильтруются всегда; отсутствие
публичного IPv6 uplink на хосте означает обычную недоступность такого назначения,
а не отключение изоляции или запрет работоспособного IPv4 sandbox. Preflight
проверяет enforcement, release gate — реальную controlled dual-stack topology.

`RLIMIT_CORE=0` и короткий bounded teardown grace являются фиксированными security defaults. Повторно использовать `CORE_AGENT_BUDGET_CANCEL_GRACE_SECONDS` при очистке дерева runtime, не добавлять второй task cancellation budget. `CHAT_WORKSPACE_ROOT`/`LOCAL_WORKSPACE_ROOT` принадлежат файловому/composition этапу; launcher получает готовые пути.

- [ ] Native preflight проверяет Linux, поддерживаемые amd64/arm64, нужные бинарники/libseccomp, read-only policy integrity, namespace creation, gate, nft write/readback, helper seccomp, resource application и конечные cgroup CPU/memory/pids limits. Запускается доверенный маленький probe, **не** пользовательская команда. Public reachability проверяется release smoke, а не превращается в бесконечный startup retry.
- [ ] Отрицательные tests: отсутствующий bwrap/nft/slirp, userns запрещён, неправильный profile digest, startup timeout, неизвестная architecture. Для каждого target marker file отсутствует; исключение `EXECUTION_ENVIRONMENT_UNAVAILABLE`, все helpers и descriptors закрыты.
- [ ] В тестовом adapter разрешён fake process для unit tests; production/development tools на несовместимой OS отвечают environment unavailable. Не предоставлять runtime flag, возвращающий прямой `Popen(argv)`.

## Задача 2. Gated namespaces, seccomp и helpers

**Files:** `core_agent/sandbox.py`, `core_agent/sandbox_exec.py`, `deploy/security/*`, `tests/test_sandbox_linux.py`.

- [ ] Запускать отдельный single-threaded trusted supervisor через текущий agent interpreter: `python -I -m core_agent.sandbox --supervise`. Передавать bounded JSON launch description и только необходимые FD по анонимным pipes. Не применять `preexec_fn` в многопоточном server. Supervisor и nft/slirp helpers получают фиксированный `PATH` и `LANG`, без provider/DB/Keycloak/OTel credentials, `LD_*`, пользовательского `PYTHONPATH` и лишних inherited FD.
- [ ] Сохранить работающую последовательность prototype: `bwrap --unshare-user --unshare-pid --unshare-ipc --unshare-net --unshare-uts --cap-drop ALL --die-with-parent --new-session --info-fd … --block-fd …`; gate остаётся закрытым. Parent validates child PID принадлежность и открывает pidfd/namespace FDs пока процесс жив; числовой PID из старого checkpoint не применяется.
- [ ] Mount только trusted read-only executables/libraries/CA certificates, проверенный chat как `/workspace`, отдельный read-only `/workspace/attachments`, private tmpfs `/tmp`, отдельный `/dev`, новый `/proc` и generated read-only resolver. Source directories открыть no-follow и bind через удержанные FD; требуемые версии bwrap с FD-bind входят в preflight. Не bind `/`, `/app`, общий chat root, `DURABLE_STORAGE_ROOT`, server `/tmp`, `/run`, `/var/run`, service-account token или Docker sockets. Python broker — ровно один socket bind в `/run/core-agent/broker.sock`; trusted trampoline — отдельный read-only файл `/run/core-agent/sandbox_exec.py`. Текущие secrets для **одного** разрешённого process добавляются только в target environment.
- [ ] Target `HOME=/workspace`, `TMPDIR=/tmp`, `PATH=/usr/local/bin:/usr/bin:/bin`; `cwd` переводится в `/workspace/<relative>` после безопасной проверки. User site-packages остаются в chat HOME. Agent `.venv` не монтируется приложению; `/usr/local/bin/python3 -P` соответствует terminal interpreter. CA/DNS mounts формируются runtime, модель их не заменяет.
- [ ] Через `ctypes` + установленную `libseccomp` компилировать inner policy в sealed memfd и передавать bwrap `--seccomp FD`. Использовать syscall names и native architecture; unsupported ABI запрещён. Outer OCI policy разрешает нужный trusted namespace bootstrap, inner policy применяется **после** него и наследуется exec/subprocess.
- [ ] Inner default-deny allowlist покрывает обычные terminal/Python/file/network operations. Разрешать `clone` только когда все `CLONE_NEW*` bits равны нулю; `clone3` возвращает `ENOSYS` для libc fallback, не разрешается с непроверяемой pointer-структурой. Запретить `unshare`, `setns`, mount API (`mount`, `umount2`, `pivot_root`, `open_tree`, `move_mount`, `fsopen`, `fsconfig`, `fsmount`, `fspick`, `mount_setattr`), `ptrace`, `process_vm_readv/writev`, `pidfd_getfd`, `open_by_handle_at`, `bpf`, `perf_event_open`, keyring, kernel module и io_uring APIs. Socket families только AF_UNIX/AF_INET/AF_INET6; raw/packet/netlink sockets запрещены. Запретить PTY injection ioctl `TIOCSTI`/`TIOCLINUX`.
- [ ] Не добавлять `--disable-userns` механически: он создаёт дополнительную вложенную user namespace и меняет отношение owner-userns/network namespace. Выбранный baseline запрещает создание namespace через inner seccomp; trusted nft helper входит именно в user namespace, владеющую netns, до release. Tests доказывают, что target не способен открыть новый userns и вернуть `CAP_NET_ADMIN`.
- [ ] `nsenter --preserve-credentials --user=<owned-fd-path> --net=<owned-fd-path> nft -f -` устанавливает полный ruleset одним transaction и завершает работу. Проверить kernel readback ruleset перед release. Запустить slirp с `--configure --disable-host-loopback --disable-dns --enable-ipv6 --enable-sandbox --enable-seccomp --ready-fd … --exit-fd …`, без API socket/port forwarding. Его FS view и открытые FD проверить в integration test; sandbox helper не должен видеть platform files через host `/etc`/`run` — Pod не размещает там credentials.
- [ ] У Bubblewrap `--block-fd` **EOF тоже разблокирует**, а inner filter устанавливается после этого read. Поэтому это только bootstrap barrier, не fail-closed exec authorization. Target bwrap command — `/usr/local/bin/python3 -I -S /run/core-agent/sandbox_exec.py`; изолированный interpreter не импортирует workspace/user-site/PYTHONPATH. Trampoline наследует отдельный private exec-gate read FD и READY write FD; никакой helper/application не получает write end gate.
- [ ] После nft readback/slirp readiness supervisor открывает bootstrap barrier. Bwrap применяет seccomp и запускает trusted trampoline; тот ставит rlimits, проверяет capabilities/NoNewPrivs, пишет READY и ждёт **ровно 32 байта release token** в отдельном exec gate. Supervisor генерирует token через `secrets.token_bytes(32)` и передаёт ожидаемое значение trampoline в private bounded config FD. EOF, short/wrong token и timeout завершают trampoline без `execve`. Только после READY supervisor одним проверенным `os.write` посылает token; trampoline закрывает gate/config FD и исполняет untrusted argv. Status FD помечен close-on-exec: trusted `execvpe` error публикует errno до выхода, успешный exec закрывает FD автоматически. До abort сначала kill/reap namespace с удерживаемым bootstrap gate, затем закрывать pipes. Проверить parent SIGKILL на каждом pre-release промежутке, включая между открытием двух gates; после final release проверять teardown/reconciliation, а не обещать отсутствие уже возможного side effect.

Минимальная критическая граница trampoline после разбора trusted config и установки limits:

```python
os.write(ready_fd, b"READY")
if os.read(release_fd, 32) != expected_token:
    os._exit(125)
os.close(release_fd)
os.close(config_fd)
os.set_inheritable(ready_fd, False)
try:
    os.execvpe(argv[0], argv, target_env)
except OSError as error:
    os.write(ready_fd, str(error.errno).encode("ascii"))
    os._exit(125)
```

Supervisor держит конечный общий startup deadline и убивает tree при отсутствии READY/release progress; short write token является setup failure. В production коде FD list фиксирован и проверен, а READY/error frames ограничены и различаются; этот пример показывает exact-token/EOF boundary, не заменяет весь launcher.
- [ ] Pre-dispatch failure определяется отсутствием финального release, не `Popen` success. Потеря slirp после release останавливает target, не включает host networking. Правила gate проверены по [Bubblewrap source](https://raw.githubusercontent.com/containers/bubblewrap/main/bubblewrap.c); игнорирование результата `read(opt_block_fd, …)` делает одногейтовый prototype недостаточным для fail-closed старта.

[Bubblewrap manual](https://raw.githubusercontent.com/containers/bubblewrap/main/bwrap.xml) документирует gate/status/seccomp и namespace behavior; [slirp4netns manual](https://github.com/rootless-containers/slirp4netns/blob/master/slirp4netns.1.md) — readiness/exit FD и helper sandbox. Приведённая последовательность — проектное применение этих primitives, которое необходимо подтвердить tests. Флаг `--disable-host-loopback` сам по себе не закрывает всю внутреннюю сеть.

## Задача 3. Enforcement публичного egress

**Files:** `core_agent/sandbox.py`, `core_agent/sandbox-policy.json`, `tests/test_sandbox.py`, `tests/test_sandbox_linux.py`.

- [ ] Хранить versioned CIDR tables на основе IANA IPv4/IPv6 special-purpose registries, с датой/источником и checksum. Генерация rules использует `ipaddress.ip_network`, без приватных атрибутов stdlib и без запроса реестра при каждом launch. Global-unicast allow исключает multicast, reserved, documentation, loopback, private, link-local, shared address space и translation/tunnel ranges, способные скрыть непубличный destination; явно запрещены IPv4-mapped обходы и NAT64/6to4/Teredo. Узкие IANA global exceptions не должны случайно превращаться в разрешение широкого special-purpose блока.
- [ ] Создавать `table inet core_agent` с default-drop input/output/forward. Собственный loopback разрешён; bridge/gateway/neighbor адрес **не** считается loopback. Для внешнего трафика сначала deny deployment CIDRs, затем immutable nonpublic destinations, затем разрешения TCP/UDP к public unicast. Сохранить только необходимые IPv6 neighbor-discovery/control packets на tap с конкретными типами и hop-limit; это не разрешение TCP/UDP к IPv6 gateway/link-local.
- [ ] DNS UDP/TCP 53 разрешён только к configured literal public resolver IP; slirp DNS отключён, cluster resolver не используется. Resolver не расширяет разрешённые назначения: nft проверяет фактические пакеты после любого DNS ответа. Public DNS-over-HTTPS не обходит destination filter. Нельзя разрешать connection только потому, что hostname был разрешён раньше.
- [ ] Поддерживаемые внешние transport protocols этого среза — TCP и UDP с IPv4/IPv6; HTTP/HTTPS, curl, requests и package managers работают без proxy env. Raw sockets, packet injection и произвольные tunnel protocols не рекламируются. IPv6 public connectivity проверяется отдельным gate; её отсутствие не маскируется IPv4 результатом. Дополнительный IPv4-only профиль требует явно описанного deployment contract, он не создаётся этим планом.
- [ ] При изменении deployment CIDRs перестроить профиль только через доверенную конфигурацию/rollout; действующий sandbox не получает права изменить правила. Пока адреса pod/service/node и protected internal services не заданы, production startup отклоняется. Нельзя вывести все защищённые публичные addresses из одного RFC1918 списка.
- [ ] Проверять counter/receiver outcome, а не только `connect_ex != 0`: в Linux fixture реально слушающий запрещённый target должен получить ноль соединений. Разрешённый контрольный target должен подтвердить соединение; иначе network outage ошибочно выглядит как изоляция.

Источники CIDR semantics: [IANA IPv4 registry](https://www.iana.org/assignments/iana-ipv4-special-registry/), [IANA IPv6 registry](https://www.iana.org/assignments/iana-ipv6-special-registry/). Syscall filtering не заменяет destination enforcement; [Linux seccomp documentation](https://docs.kernel.org/userspace-api/seccomp_filter.html) описывает его ограничения.

## Задача 4. Ресурсы и подтверждённый teardown

**Files:** `core_agent/sandbox.py`, `core_agent/execution.py`, `tests/test_sandbox_linux.py`.

- [ ] Single-threaded trusted trampoline применяет native hard rlimits после seccomp и до READY/final target release; application не может увеличить hard values. Сам supervisor/helpers имеют отдельный ограниченный bootstrap budget, не наследуют user environment и не расходуют target CPU quota на подготовку. Для всего Pod требуются конечные `cpu.max`, `memory.max` и `pids.max`; прочитать фактические значения, не считать YAML единственным доказательством. Semaphore ограничивает число одновременно работающих sandboxes, PTY output остаётся bounded существующим reader.
- [ ] Не использовать `RLIMIT_RSS` как memory enforcement. `RLIMIT_NPROC` считается по real UID и имеет исключения, включая UID 0; prototype запускается с namespace UID 0, поэтому этим лимитом нельзя доказать per-sandbox process ceiling. Выбранный baseline ограничивает aggregate process count конечным Pod `pids.max`, а CPU/AS — дополнительно per process. Он **не** обещает fair-share или отдельную cgroup каждого чата. Если acceptance потребует именно такие per-chat quotas, без узкой delegated cgroup capability этот профиль не объявляется достаточным.
- [ ] Следить отдельно за target completion и namespace lifetime: bwrap init может ждать оставшихся descendants после завершения initial process. При target exit, timeout, cancel, session destroy, helper death или parent death завершать namespace init через живой pidfd; kernel уничтожает его descendants, включая `setsid`/double-fork. Затем дождаться bwrap и slirp, закрыть broker, pipes/PTY и ephemeral launch dir. Не полагаться только на `killpg` и не возвращаться раньше cleanup лишь потому, что stdout уже закрыт.
- [ ] Process handle не переводится в `cleanup=process_group_terminated` без подтверждения. Повторный stop безопасен; истёкший PID никогда не берётся из persisted checkpoint. Неудачный teardown делает execution subsystem unhealthy, блокирует следующий writer этого workspace и требует recovery; не удаляет workspace.
- [ ] После release и неизвестного исхода target ошибка остаётся `SIDE_EFFECT_UNKNOWN` для mutating operation; запретить blind replay. До release ошибка доказанно pre-dispatch и возвращается модели как `EXECUTION_ENVIRONMENT_UNAVAILABLE`. Обычные nonzero exit/timeout сохраняют существующий structured tool result и возможность объяснить/исправить проблему.

Реальная граница rlimits описана в [Linux getrlimit](https://man7.org/linux/man-pages/man2/getrlimit.2.html); Pod PID limit задаётся kubelet, а не полем `resources.limits.pids`: [Kubernetes PID limits](https://kubernetes.io/docs/concepts/policy/pid-limiting/). Не выдавать aggregate Pod limit за независимую квоту чата.

## Задача 5. Docker/Kubernetes и первый обязательный Linux gate

**Files:** `Dockerfile`, `deploy/security/*`, `deploy/kubernetes/*`, `.github/workflows/ci.yml`, `tests/test_sandbox_linux.py`.

- [ ] Установить `bubblewrap slirp4netns nftables libseccomp2 util-linux` в существующий image; записать проверяемые versions в build output. Profiles и policy root-owned/read-only; workspace/runtime scratch writable `10001`. Не переносить `/tmp` prototype credentials/config в image. Обе архитектуры получают отдельный проверенный OCI profile из закреплённого upstream baseline; не менять JSON architecture tag без пересборки/проверки syscall policy.
- [ ] Зафиксировать Pod constraints в deploy manifest:

```yaml
spec:
  hostUsers: false
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
  containers:
    - name: agent
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
        procMount: Unmasked
        seccompProfile:
          type: Localhost
          localhostProfile: core-agent/oci-amd64.json
      resources:
        requests: {cpu: 250m, memory: 512Mi}
        limits: {cpu: "2", memory: 4Gi}
```

- [ ] Manifest mounts только chat PVC, immutable durable storage по существующему deployment contract, private scratch `emptyDir` и узкий `/dev/net/tun` CharDevice для trusted helper. TUN не bind-ится в application sandbox. Не монтировать host root/cgroup/runtime sockets и не давать privileged/SYS_ADMIN Pod. Kubelet `podPidsLimit=512` в kind fixture и конечный подтверждённый аналог на целевом кластере.
- [ ] Localhost seccomp устанавливает администратор node image/конфигурации; agent Pod не устанавливает профиль себе через host root mount. kind fixture включает exact file mapping. Поддержка `/dev/net/tun`, Localhost seccomp, Unmasked proc, userns, AppArmor/SELinux и idmapped volumes — prerequisites, не silently optional настройки.
- [ ] В normal CI добавить blocking job `sandbox-linux` после image build: pinned kind/node image с userns support, явный kubeconfig в `$RUNNER_TEMP`, загрузка image, запуск test Pod с production-equivalent securityContext. В test image копировать authorized tests; production image не раздувать test dependencies. Команда Pod: `uv run --no-sync python -m unittest tests.test_sandbox_linux -v`. `CORE_AGENT_REQUIRE_SANDBOX_TESTS=1` превращает отсутствующую Linux capability, fixture или skipped test в failure.
- [ ] Dual-stack fixture обеспечивает controlled публично классифицируемые IPv4/IPv6 destinations и реальные private listeners. Допустимо использовать routes/address aliases **только внутри одноразовых CI network namespaces/node containers**; не назначать публичные адреса физической сети. Проверка public HTTPS использует реальный HTTPS endpoint отдельно от controlled adversarial topology. Redirect/rebinding fixture меняет DNS/Location на работающий private listener; policy должна блокировать новый destination.
- [ ] Проверять native amd64 в обычном Linux CI и arm64 на native runner/доступной ARM среде; эмуляция не считается доказательством kernel/ABI seccomp. До arm64 gate release advertisement ограничивается реально проверенной архитектурой. Не заменять failed gate на skip или `continue-on-error`.
- [ ] Первый commit: `feat: add gated Linux sandbox launcher and egress checks`; не менять runtime registration и не помечать enterprise execution implemented.

Текущие [Kubernetes user namespace requirements](https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/) требуют совместимых runtime/kernel и idmapped mounts. `procMount: Unmasked` не соответствует Restricted Pod Security даже с `hostUsers:false`; нужны согласованные узкие deployment permissions. NFS/idmap и конкретный CSI проверяются отдельно. Успех локального kind не доказывает этих условий на целевом кластере.

## Задача 6. Подключить все callers и закрыть terminal/recovery гонки

**Files:** `core_agent/execution.py`, `core_agent/python_exec.py`, `core_agent/tools.py`, `core_agent/runtime.py`, `core_agent/app.py`, существующие execution/recovery tests.

- [ ] Передавать shared launcher из `app.py` в `LocalTerminalBackend`; `_LocalTerminalSession.start` использует его для **каждого** command. Сохранить typed argv validation, explicit-shell policy, timeout/output redaction и существующий PTY API. Никакого `try sandbox … except: Popen(argv)`.
- [ ] Доверенное binding workspace/session регистрируется после admission, при initialize/restore и до первой команды. `execute_transient` больше не создаёт `EnvironmentSpec("default", run_id, …)` при отсутствующем scope: fail closed. Child `_child_agent` использует тот же manager; background `_task_start` и `_recover_background_tool` передают immutable scope из saved contract. Model `cwd` не выбирает другой workspace.
- [ ] `execute_python` создаёт собственный `PythonToolBroker`, передаёт socket как internal launcher kwarg и подменяет runner path на sandbox-visible socket. Token не попадает в checkpoint/logs; broker закрывается даже при bootstrap failure. `tools.call` остаётся в parent runtime и повторяет capability/schema/budget/policy checks. `without_terminal` не отключает sandbox Python или его subprocess.
- [ ] Добавить единый idempotent cleanup owned execution tree до записи terminal outcome root/child. Сейчас `_drop_run_runtime` удаляет только caches/MCP connectors, а cancel отдельно вызывает `destroy_run`; простого добавления cleanup **после** terminal недостаточно, потому что schema-13 admission уже увидит свободный чат. Использовать canonical terminal transition path и сохранённое parent/background ownership, не имена run-ID с угадыванием prefix.
- [ ] Не удерживать PostgreSQL transaction во время signals/waits. Пока cleanup идёт, canonical workflow остаётся nonterminal и чат занят; после подтверждения отсутствия owned processes выполнить обычную fenced terminal transaction. Inbound terminal race сохраняет существующую обработку inbox. При cleanup failure не освобождать write barrier или сохранять ложный completed; переход/reconciliation выбирается существующими side-effect rules.
- [ ] Учесть budget-partial: результат может перечислять неподтверждённые remote/scheduler tasks, однако owned local execution не должен оставаться writer после terminal. Local process teardown и downstream cancel acknowledgement — разные условия. Если local cleanup не подтверждён, хранить nonterminal/reconciliation, не объявлять чистый budget completion.
- [ ] Handle ownership включает `(run_id, worker_id, execution_generation)` из текущего workflow lease/scheduler claim. При `LEASE_LOST` остановить только handles данной generation, не terminalize workflow и не убивать нового владельца. Не вызывать голый `destroy_run(run_id)` из stale worker после появления новой generation. Namespace parent-death protection и fail-closed exec gate закрывают crash до cleanup. Recovery `EXECUTING` сохраняет reconciliation, не replay; безопасный recovery создаёт новый sandbox с тем же trusted workspace. Старый persisted PID не используется для kill.
- [ ] Graceful shutdown: остановить scheduling/admission новых executions, закрыть root/child/background handles и brokers bounded grace, затем manager/helpers, MCP и DB. `CoreAgent.close`/`app.close` вызывают один shared owner cleanup, а повторный close ничего не ломает. Не удалять persistent chat folder в `_LocalTerminalSession.destroy`; это согласованный файловый integration seam.
- [ ] Второй commit: `feat: enforce sandbox execution and owned process cleanup`; обновить AGENTS/README по фактическому поведению и release evidence только после gates.

## Проверки, которые отличают изоляцию от сборки argv

В `tests/test_sandbox_linux.py` tests выполняют production launcher/handlers и проверяют effects. Перед негативной проверкой fixture доказывает доступность target доверенному control process.

| Test | Обязательный наблюдаемый результат |
|---|---|
| Два одновременно живых чата | Оба меняют свой sentinel; соседний workspace, server secret file и broker socket недоступны через absolute path, `..`, symlink, `/proc/*/root`, `/proc/*/fd` и `/proc/*/environ`. Input mount нельзя rename/replace/unmount; копирование input в RW tree работает. |
| Inner seccomp | `clone(CLONE_NEWUSER/NEWNET/NEWNS)`, `unshare`, `setns`, mount APIs, ptrace/process_vm, AF_PACKET/AF_NETLINK/SOCK_RAW отклонены; обычные Python threads, fork/exec, requests, pip user install и compiler subprocess работают. |
| Namespaces/capabilities | Имена namespace отличаются от server; proc видит только sandbox; `CapEff/CapPrm/CapAmb` нулевые, NoNewPrivs установлен; app FD list не содержит gate, namespace, nft/slirp control или server descriptors. |
| Public egress | Реальные IPv4 и IPv6 TCP/UDP control targets доступны, HTTPS certificate verification работает; DNS и package-manager download используют разрешённый маршрут без proxy env. |
| Private egress | Слушающий RFC1918, ULA/link-local, metadata, node/Pod/Service, gateway-host-loopback и protected-public-CIDR target получает ноль connections/datagrams. IPv4-mapped IPv6, integer/alternate URL IPv4, redirects, DNS answer changes и снятые proxy env не обходят правила. |
| Policy tamper | App не меняет nft, routes, resolver mount, namespace membership или helper control FD; nft counters/readback до/после совпадают с ожидаемыми. |
| Bootstrap/helper failure | Каждый этап до release и slirp death после release убирают всё дерево; до release target marker не создан, после release mutating outcome не replay-ится. |
| Resource limits | Hard rlimits реально ограничивают alloc/open/write/CPU; controlled bounded child spawn достигает cgroup pids ceiling; test Pod имеет конечные CPU/memory/pids. Не делать fork bomb на общей машине. |
| Teardown | Parent, grandchild, double-fork/setsid и процесс с закрытым stdout отсутствуют после timeout/cancel/normal return/helper crash. Повторный stop, client disconnect, root/child completion и app shutdown сохраняют correct ownership. |
| DB lifecycle | Реальный PostgreSQL: terminal не коммитится до local teardown; concurrent новый root остаётся busy; lease loss не меняет outcome и не убивает чужой worker; restart EXECUTING даёт reconciliation без второго marker. |
| Python broker | Собственный token работает; чужой socket/token и выбранный моделью tenant/chat не дают вызов; nested tool policy/budget по-прежнему проверяются; bootstrap/timeout закрывает endpoint. |

Targeted commands после соответствующего implementation task:

```bash
uv run python -m unittest tests.test_sandbox -v
uv run python -m unittest tests.test_local_terminal tests.test_python_exec tests.test_tasks_tools_execution -v
uv run python -m unittest tests.test_runtime_observability tests.test_postgres_persistence -v
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
uv build --no-sources
docker build -t core-agent:sandbox .
git diff --check
```

PostgreSQL commands требуют настоящий `TEST_DATABASE_URL`; основной suite также использует существующий Keycloak CI service. Linux integration command выполняется дополнительно внутри указанного kind Pod; native workstation mock/unit success его не заменяет. В job дождаться завершения Pod через bounded `kubectl wait --for=jsonpath='{.status.phase}'=Succeeded … --timeout=180s`, сохранить logs и exit status; Failed/timeout/skipped gate является failed check. Cleanup CI удаляет только созданные им cluster/resources.

## Что остаётся release blocker после локальной реализации

- Целевой cluster должен пройти тот же набор с реальным CSI/PVC, node kernel, runtime, AppArmor/SELinux, Seccomp admission и защищёнными CIDRs; узкие TUN/proc/profile permissions предварительно проверяются. Не подменять отказ privileged mode или unconfined seccomp.
- Dual-stack egress, helper confinement, syscall compatibility и process-tree cleanup пока **не** доказаны prototype. Если один из них не проходит, unsupported capability возвращает `EXECUTION_ENVIRONMENT_UNAVAILABLE` и release gate остаётся красным.
- Shared Pod resource envelope не обещает per-chat hard cgroup quotas или защиту от всех kernel vulnerabilities. Resource saturation может остановить Pod; durable runtime recovery и отсутствие повторения неизвестных side effects сохраняются обязательными.
- Atomic incoming file publication и guardrail quarantine завершаются отдельным файловым этапом: sandbox предоставляет mount boundary, но не делает PostgreSQL commit и filesystem rename одной транзакцией. Живой sandbox немедленно увидит файл в смонтированной chat directory; rejected/pending material нельзя публиковать туда в расчёте на последующий rollback.


### Generation ownership и ошибки до запуска

`ExecutionNotStarted` отличает доказанный отказ до release от неизвестного
результата исполнения, сохраняя публичный error code. Startup после release
по-прежнему требует reconciliation. Manager привязывает session к worker и
execution generation, закрывает только captured handles, не открывает retired
попытку при follow-up. Сериализованный teardown не публикует второй snapshot;
nonterminal lease exit сохраняет ephemeral файлы run-а. Начальная папка и
server-owned marker публикуются вместе после успешного snapshot materialization.

Совместный focused gate manager/sandbox/Python/approvals: 85 tests, exit 0; Ruff
для затронутых файлов проходит. Дополнительный workspace integration запуск
не прошёл: здесь запрещён AF_UNIX bind реального Python broker (`EPERM`).
Durable terminal intent и operational cleanup receipts подключены к root,
child и background workflow. Terminal commit следует только после подтверждённой
очистки canonical subtree; intent блокирует новые admissions. Foreign receipt
с возможным local execution сохраняет busy/reconciliation, а явное отсутствие
pending local execution позволяет восстановить model-only и безопасное ожидание.
Follow-up переоткрывает работу только с новой generation; retired intent и stale
finally не закрывают её процессы, cache или connector. Memory/PG stores используют
одинаковый контракт; проверки реальных PostgreSQL locks ещё требуют тестовой БД.

Финальный focused receipt/cache gate: 80 tests, exit 0, 6 PostgreSQL skips;
Ruff прошёл. Общий прогон: 677 tests, 7 failures, 35 errors, 144 skips.
Обнаруженные устаревшие fixtures background admission и описания sandbox
исправлены отдельно; остальные сбои связаны с запрещёнными socket operations.
Это не зелёный release gate и не доказательство native sandbox свойств.


## Состояние подключения — 30 сентября 2026

`create_app` подключает обязательный preflight и один launcher к terminal,
Python и background target. Hook `on_start(session, handle)` и trusted
`broker_socket` не являются model arguments. Process reaper работает независимо
от PTY drain; ошибка cleanup не превращается в успешный result и запрещает
snapshot/delete. Readiness закрыта для unhealthy launcher. Для portable unit
suite `tests/app_support.py` явно инжектирует adapter; native test использует
реальный composition root и не импортирует этот helper.

Нормативная inner policy теперь находится в `core_agent/sandbox-policy.json`:
единственный файл попадает в wheel/sdist как package data и отдельно копируется
в root-owned read-only `/opt/core-agent/security` образа. SHA256 в launcher
сохраняется. `uv build --no-sources` успешно построил оба артефакта; наличие и
точные bytes policy проверены внутри обоих архивов. `docker compose config
--quiet` проходит; все SANDBOX settings передаются сервису без permissive
fallback для отсутствующего списка защищённых CIDR.

Blocking `sandbox-linux` CI job после `verify` содержит native amd64/arm64
matrix, закреплённые kind executable/node, профиль внутри одноразового node,
реальные dual-stack receiver counters и обязательный Pod success. Проверки YAML
и генерации fixture environment проходят; bounded review не выявил дефектов.
Docker/Kubernetes job в этой среде не выполнялся. Unit success не доказывает
namespace/egress/CSI свойства.

Focused integration gate: 125 tests, 89 passed и 36 skipped. Полный suite на
этом этапе: 630 tests, 40 socket-permission errors, один assertion о причине
заблокированного соединения, 139 skipped. Release gate остаётся незавершённым.
Подключение durable Python nested wait выполняется следующим срезом после
подтверждённой остановки процесса; launcher integration не заменяет его proof.
