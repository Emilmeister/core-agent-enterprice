# Cloud.ru Landlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** добавить явно выбранный ограниченный Landlock backend только для Cloud.ru development, сохранив Bubblewrap по умолчанию.

**Architecture:** Landlock ограничивает filesystem, строгий seccomp выдаёт разрешённые socket FD через user notification доверенному HTTP proxy и собственному Python broker. Trusted subreaper удерживает отдельный PGID, а приложение PID 1 выделенного контейнера обеспечивает внешний lifetime barrier. Реальные workspace/HOME и частный TMPDIR заменяют виртуальные mounts; execution с непустым attachments отклоняется.

**Tech Stack:** Python/stdlib/ctypes, существующий libseccomp и SandboxPolicy, Linux Landlock ABI >= 4, seccomp NEW_LISTENER/ADDFD, unittest, uv.

Нормативный источник — [CLD-05-L](../../../spec/cloudru.md#cld-05-l-явный-development-backend-landlock),
критерии — CLD-AC-14–17. Пользователь разрешил связанные spec/tests; работа
в постоянном worktree `.enterprise-work/cloudru-ai-agents`, ветка
`feat/cloudru-ai-agents`. Этот план заменяет выбор исключительно Bubblewrap
в предыдущем плане только для явного Cloud development opt-in. S3, production,
read-only attachment migration и произвольный direct network в задачу не входят.

### Task 1: Зафиксировать контракт и startup selection

**Files:** `spec/cloudru.md`, `spec/acceptance.md`, `spec/agent-configuration.md`,
`spec/execution-environment.md`, `spec/security-and-reliability.md`,
`spec/README.md`, `spec/releases/v1.md`, `spec/implementation-status.md`,
`tests/test_spec_lock.py`, `core_agent/app.py`, `.env.example`.

- [ ] Сохранить narrow exception CLD-05-L, CLD-AC-14–17 и статус `missing` до proof.
- [ ] Проверить `SANDBOX_BACKEND`: default `bubblewrap`; неизвестное значение
  и `landlock` вне `development` + `cloudru` дают `CONFIG_INVALID`.
- [ ] Перед serving вызвать реальный `preflight()` выбранного launcher;
  не менять backend после отказа и сохранить injectable test launcher.

### Task 2: Ограничить files/syscalls и проверить реальные kernel операции

**Files:** `core_agent/landlock_policy.py`, `tests/test_landlock_policy.py`.

- [ ] Добавить tests для ABI < 4, missing features, no_new_privs, non-root UID, zero
  effective/permitted/inheritable/ambient capabilities и ошибочного PID-1 layout.
- [ ] Применить Landlock к конкретным workspace/private temp/read-only runtime
  paths; проверить соседний чат, server secrets, host /tmp и symlink escape.
- [ ] Строгим seccomp закрыть namespace/process escape, setsid/setpgid,
  ptrace/process memory/descriptors и direct/alternate socket syscalls.
- [ ] Реально проверить NEW_LISTENER/ADDFD: ограниченный child получает
  разрешённый FD, запрещённая операция получает отказ, все probe процессы убраны.

Run: `uv run python -m unittest tests.test_landlock_policy -v`.
Expected: обычные проверки проходят; native proof выполняется на Linux, его
отсутствие не записывается как доказательство готовности backend.

### Task 3: Соединить proxy, Python broker и безопасную metadata mediation

**Files:** `core_agent/landlock_network.py`, `tests/test_landlock_network.py`,
`core_agent/landlock_metadata.py`, `tests/test_landlock_metadata.py`.

- [ ] Создать bounded trusted HTTP/HTTPS CONNECT proxy; выдавать socket FD
  только через проверенную seccomp notification, собственный broker — отдельно.
- [ ] Переиспользовать SandboxPolicy для DNS servers, всех DNS answers и
  подключённого адреса; проверить denied/mixed/rebound/IPv6/mapped targets.
- [ ] Проверить HTTP/CONNECT успех, direct IP/UDP/Unix/NO_PROXY отказ,
  redirects, чужой broker, limits и cleanup без credential leakage.
- [ ] Обрабатывать chmod/timestamps через bounded копирование caller arguments
  и закреплённые root/file FD без CONTINUE; проверить symlink/directory swap,
  чужой FD, mode bits вне 0777 и настоящий Git init/clone. Fchmod своего FD
  разрешает только 0777, runtime root-owned/immutable; chown/xattr/inotify и
  отправка сигналов остаются закрыты.

Run: `uv run python -m unittest tests.test_landlock_network tests.test_landlock_metadata -v`.
Expected: разрешённый transport работает, обходы закрыты, неопределённость
передачи FD не разрешает запуск либо новый side effect.

### Task 4: Включить lifecycle и существующий execution flow

**Files:** `core_agent/sandbox_landlock.py`, `core_agent/execution.py`,
`core_agent/python_exec.py`, `core_agent/runtime.py`, `core_agent/app.py`,
`core_agent/chat_files.py`, `tests/test_sandbox_landlock.py`,
`tests/test_landlock_chat_files.py`.

- [ ] Сохранить launcher `start/preflight/close` и process `wait/poll/stop/pid`
  contract; каждый execution получает trusted subreaper и фиксированный PGID.
- [ ] До запуска отклонить непустой attachments без chmod, перемещения или
  удаления bytes; передавать настоящий HOME/cwd, private TMPDIR и scoped broker.
- [ ] В общем file admission отклонить новые protected owner uploads с
  CAPABILITY_DISABLED до принятия, а прежнюю quarantine publication — до writes;
  проверить background race и сохранность history/bytes/owner access. Generated
  files и writable `a2a/` сохраняют контракт.
- [ ] Поддержать `/workspace/...` только в проверенном tool cwd; не переписывать
  пользовательские argv/source; согласовать tool descriptions и runtime paths.
- [ ] Проверить terminal/Python/tools.call/background/nested calls, timeout,
  cancel, normal exit с orphan, смерть supervisor/PID1 и запрет setsid/setpgid.
- [ ] Подтверждать полный tree cleanup до `sandbox_terminated`; при unknown
  outcome сохранить SIDE_EFFECT_UNKNOWN, disabled launcher и no-replay barrier.

Run: `uv run python -m unittest tests.test_sandbox_landlock tests.test_landlock_chat_files -v`.
Expected: regression проходит; kernel/lifetime свойства подтверждаются native
тестами в dedicated container с PID-1 приложением, не mock результатом.

### Task 5: Проверить совместимость и передать ограниченный trial

**Files:** `docs/cloudru-trial.md`, `AGENTS.md`,
`spec/implementation-status.md`, `.github/workflows/ci.yml` при добавлении gate.

- [ ] Запустить `uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v`
  и `git diff --check`; сохранить точные bytes/hash lock и рабочие ссылки.
- [ ] Запустить `uv run ruff check core_agent tests` и
  `uv run python -m unittest discover -s tests -v`.
- [ ] Выполнить native Landlock suite/preflight в dedicated container;
  required gate не допускает skip вместо proof. Сохранить прежний Bubblewrap gate.
- [ ] Собрать `docker build --platform linux/amd64 -t core-agent:cloudru-landlock .`.
- [ ] Передать оператору ENV/ограничения из trial guide; настоящий Cloud Pod
  должен отдельно пройти preflight, terminal/Python/background и negative probes.
- [ ] Статус обновлять только по вошедшему в обычный CI automated proof;
  target Cloud gate отражать отдельно. До завершения проверок не объявлять
  `implemented` или production readiness. Коммит делает основной агент после
  интеграции всего логического изменения; параллельные участники не коммитят.
