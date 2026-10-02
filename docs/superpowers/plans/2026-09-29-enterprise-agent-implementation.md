# Enterprise Core Agent — порядок реализации и план подготовки контрактов

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать согласованное ТЗ: общий UI владельцев, Keycloak, изолированные чаты и файлы, HITL, долгие A2A-операции, cron, guardrails и семантическую суммаризацию.

**Architecture:** Сохранить существующие A2A Task lifecycle, PostgreSQL workflow/inbox/outbox, leases и централизованный tool dispatch. Добавить авторизованный доступ владельцев и внешних агентов, постоянные папки чатов и Bubblewrap; ожидания сохранять в БД и освобождать worker.

**Tech Stack:** Python 3.12+, uv, существующие A2A SDK/ASGI, PostgreSQL/psycopg, Keycloak, Bubblewrap, Kubernetes. UI — React, TypeScript и Vite, обычный CSS; сборку отдаёт существующий ASGI сервис в том же Pod. Отдельная БД и брокер сообщений для этих требований не вводятся.

---

Статус: реализация начата; 29 сентября 2026 года пользователь явно разрешил
изменения `spec/**`, `tests/**` и hash lock в рамках согласованного ТЗ.
План первого этапа ниже касается нормативных
контрактов; таблица следующих этапов задаёт зависимости и проверяемый результат,
но не заменяет их подробные планы изменений кода.

Источник требований: [согласованное рабочее ТЗ](../specs/2026-09-29-enterprise-agent-design.md).
Правила работы: [AGENTS.md](../../../AGENTS.md) и
[spec-driven процесс](../../../spec/development-process.md).

## Условие начала изменений продукта

`AGENTS.md` требует отдельного явного разрешения на изменения `spec/**/*.md`,
`tests/**` и frozen hashes. Запрос начать реализацию не используется как
неявное снятие этого ограничения.

Получено разрешение в рамках согласованного ТЗ:

- переносить требования в перечисленные ниже нормативные документы;
- добавлять и изменять проверки нового поведения в `tests/**`;
- обновлять `EXPECTED_SHA256` в `tests/test_spec_lock.py` после проверки spec diff,
  сохраняя саму проверку полного набора файлов и их точных bytes.

Не требуется повторное согласование уже принятых продуктовых решений.
Разрешение не означает удаление старых пользовательских данных, публикацию
сервиса или применение миграций к рабочей БД.

## Этап 0. Нормативные контракты и границы совместимости

### 0.1. Перенести согласованные решения в источник истины

- [x] Получить указанное выше разрешение до любых правок замороженных файлов.
- [x] Перенести требования по карте ниже, сохранив их смысл, значения по умолчанию
  и сценарии отказов; устранить противоречащие старые формулировки.

| Требования рабочего ТЗ | Нормативные документы | Изменение контракта |
| --- | --- | --- |
| AUTH-01–04, UI-01–02, API-01, A2A-01 | `spec/product.md`, `spec/security-and-reliability.md`, `spec/public-contract.md`, `spec/a2a-protocol.md`, `spec/agent-configuration.md` | Общие владельцы по роли Keycloak; внешние service accounts изолированы; отдельные входы владельцев, внешних агентов и HITL; каждый новый запрос проходит авторизацию, открытый ответ повторно не проверяется |
| TASK-01–03 | `spec/a2a-protocol.md`, `spec/public-contract.md`, `spec/runtime.md`, `spec/architecture.md` | Один активный root Task на чат, новая попытка в занятом чате — отдельный failed Task; durable follow-up и идемпотентность создания по caller/messageId |
| TOOL-01–02, HITL-01–04, INPUT-01 | `spec/tools.md`, `spec/agent-configuration.md`, `spec/runtime.md`, `spec/security-and-reliability.md` | Три режима каждого tool, новые требуют HITL; решения только владельцев; default timeout 24 часа; отказ/timeout возвращаются модели; вопросы владельцам приватны |
| FILE-01–04, §12.1, NET-01 | `spec/execution-environment.md`, `spec/security-and-reliability.md`, `spec/public-contract.md`, `spec/a2a-protocol.md`, `spec/architecture.md` | Постоянная папка чата под CHAT_WORKSPACE_ROOT, Bubblewrap, публичный интернет без внутренней сети, атомарные вложения, лимит по умолчанию 25 000 000 bytes на сообщение, безопасная ручная очистка |
| LONG-01–04 | `spec/tasks-and-delegation.md`, `spec/tools.md`, `spec/runtime.md`, `spec/a2a-protocol.md` | core_agent_send_message возвращает handle; core_task_wait сохраняет продолжение; core_wait_until будится новым сообщением; timeout внешней операции окончателен |
| CRON-01–07 | `spec/tasks-and-delegation.md`, `spec/agent-configuration.md`, `spec/runtime.md`, `spec/tools.md` | Расписания через UI и отключаемый tool; один чат на расписание; пропуски без очереди; timezone, ручной запуск и изменения только будущих запусков |
| CONTEXT-01–03 | `spec/context.md`, `spec/runtime.md`, `spec/memory-service.md` | Семантическое summary и контекст между задачами; полная история без автоматического истечения; durable состояние ожиданий не извлекается из summary |
| GUARD-01–04, §12.3 | `spec/security-and-reliability.md`, `spec/tools.md`, `spec/agent-configuration.md`, `spec/kernel-instructions.md` | Изолированный LLM-детектор, карантин, решение владельца; ошибка детектора также требует решения; исключение tool охватывает аргументы и результаты; benchmark качества на данных исключён |
| CLEAN-01, SCOPE-01 | `spec/artifacts.md`, `spec/tools.md`, `spec/agent-configuration.md`, `spec/product.md` | Удаление трёх artifact tools и исключительно их инфраструктуры; A2A output artifacts сохраняются; переключение CodeAgent/ReAct не добавляется |

- [x] Согласовать `spec/README.md` с новой картой capabilities. Не заменять
  authenticated identity полями в `RunRequest` и не создавать вторую публичную
  task state machine.
- [x] Перенести все сценарии AC-01–AC-70, включая AC-17a, из рабочего ТЗ в
  `spec/acceptance.md`. Использовать префикс `ENT-AC-` для отличия от существующих
  критериев; сохранить таблицу соответствия исходным идентификаторам.
- [x] Расширить scope текущего release candidate в `spec/releases/v1.md`.
  Новые обязательства оставить невыполненными до появления доказательств;
  удалить противоречия вида «HITL отсутствует» из целевого scope.
- [x] В `spec/implementation-status.md` отделить новые обязательства от уже
  доказанного foundation behavior. Статус `implemented` не переносить на
  изменённую семантику по факту существования старого теста.

### 0.2. Зафиксировать совместимость до изменения соответствующего кода

- [x] Описать переход со старого A2A входа на два авторизованных входа, mapping
  существующих identity/context и отсутствие автоматического присвоения старых
  данных новому внешнему caller. Номер публичного application contract отделить
  от версии A2A протокола.
- [x] Определить версии persisted state и порядок миграции: chat ownership,
  создание задач/messageId, wait records, pending HITL, remote handles,
  расписания, file metadata и ссылки на содержимое. Миграция выполняется
  отдельной migration job; serving process не повышает schema version сам.
- [x] Описать восстановление legacy workflow без повторного mutating dispatch.
  Старый checkpoint не интерпретировать как новую незапущенную операцию.
- [x] Описать перенос `REMOTE_AGENTS` в управляемые владельцами настройки,
  отсутствие секретов в UI-ответах и сохранение старых файлов при удалении
  artifact service. До переноса загрузки файлов artifact backend не удалять.
- [x] Для каждого изменения схемы указать допустимый порядок обновления image/БД
  и границу отката. Не обещать запуск старого image с неизвестной ему схемой.

### 0.3. Проверить и закрепить нормативный этап

- [x] Просмотреть diff на соответствие рабочему ТЗ, в том числе различия между
  пробуждением `core_wait_until` и ожиданиями HITL/remote/ответа владельца.
- [x] Обновить только утверждённые entries `EXPECTED_SHA256` по точным bytes
  нормативных файлов. Сохранить assertions и охват всех `spec/**/*.md`.
- [x] Проверить ссылки, примеры и выполнить:

```bash
uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v
git diff --check
```

Ожидаемый результат: обе команды завершаются с exit code 0. Эти проверки
подтверждают согласованность документов, а не реализацию возможностей.

- [x] Обновить `AGENTS.md` только в части изменившихся нормативных правил,
  отделяя их от фактически подключённых runtime capabilities.
- [x] Зафиксировать проверенные изменения логическими spec commits, не смешивая
  их с несвязанными изменениями реализации.

## Порядок следующих этапов

Для каждого этапа сначала составляется подробный план по актуальному коду и
утверждённому нормативному контракту. Этап включает regression proof, подключение
в composition root и проверки; наличие отдельно написанного модуля не означает
завершение этапа. Выполнение — в текущей сессии. Подагент получает отделимый
implementation scope и явное владение файлами; основной агент отвечает за
интеграцию, проверки и принятие результата. Одновременные правки одного участка
не допускаются; независимое review не изменяет implementation.

Подробные планы по текущему коду:
[workspace/files](2026-09-30-enterprise-files.md),
[sandbox](2026-09-30-enterprise-sandbox.md),
[interactions/guardrails](2026-09-30-enterprise-interactions.md),
[owner UI](2026-09-30-enterprise-owner-ui.md),
[semantic context/history](2026-09-30-enterprise-context.md),
[trusted remote A2A](2026-09-30-enterprise-remote-a2a.md),
[cron](2026-09-30-enterprise-cron.md),
[удаление artifact tools](2026-10-01-remove-artifact-tools.md).

| Этап | Основные существующие точки изменений | Проверяемый результат и зависимость |
| --- | --- | --- |
| 1. Keycloak и доступ | `core_agent/app.py`, `core_agent/a2a_sdk.py`, `core_agent/database.py`, `core_agent/security.py`, `.env.example` | Owner role и stable service-account identity работают на HTTP границе; external caller не читает чужие объекты и не принимает HITL. Проверяются introspection failure, отзыв токена, открытый stream и новый запрос. Основа всех следующих этапов |
| 2. Приём задач, чаты и ожидания | `core_agent/workflow.py`, `core_agent/database.py`, `core_agent/a2a_sdk.py`, `core_agent/runtime.py`, `core_agent/lifecycle.py` | Атомарный busy guard, messageId deduplication, durable wait/deadline и однократное продолжение после гонки/restart. Переиспользуются существующие inbox/outbox/leases; общий механизм для HITL, remote и timer |
| 3. Workspace, файлы и sandbox | `core_agent/execution.py`, `core_agent/python_exec.py`, `core_agent/app.py`, `core_agent/artifacts.py`, `Dockerfile`, `docker-compose.yml` | Постоянные scoped папки, приём/выдача вложений, quarantine/staging, безопасные имена и лимит, Bubblewrap и контролируемый egress. Проверка на Linux и целевом Kubernetes обязательна; отсутствие sandbox не даёт fallback |
| 4. Политики, HITL и вопросы владельцу | `core_agent/tools.py`, `core_agent/config.py`, `core_agent/runtime.py`, `core_agent/workflow.py`, `core_agent/app.py` | Запрещённый tool скрыт и блокируется при dispatch, включая Python broker/child. Approve/reject/timeout, смена policy и приватность вопросов работают с recovery на механизме этапа 2 |
| 5. Первый сквозной UI | `core_agent/app.py` и новый UI, структура которого фиксируется в плане этапа | Вход владельца, общие чаты, отправка/получение файлов, статусы, HITL и вопросы внутри чата; настройки tools и trusted agents. Работает с реальными backend этапов 1–4; reconnect не запускает работу повторно |
| 6. Долгие внешние задачи | `core_agent/remote_agents.py`, `core_agent/runtime.py`, `core_agent/tasks.py`, `core_agent/postgres_tasks.py`, `core_agent/workflow.py` | core_agent_send_message/core_task_wait/core_wait_until соответствуют ТЗ. Default wait 24 часа и polling 5 минут настраиваются; после timeout нет нового polling/cancel, повторное ожидание сразу возвращает окончательный исход |
| 7. История и summary | `core_agent/context.py`, `core_agent/runtime.py`, `core_agent/model.py`, `core_agent/workflow.py` | Следующая задача видит summary и последние сообщения; после повторных compaction сохраняются актуальные решения, незавершённая работа и provenance. Ошибка summary не уничтожает прежний контекст или полный transcript |
| 8. Cron | `core_agent/runtime.py`, `core_agent/workflow.py`, `core_agent/database.py`, `core_agent/tools.py`, `core_agent/app.py` и UI расписаний | Запуски используют admission и чат этапа 2, историю этапа 7. Пропуски, overlap, ручной запуск, timezone, disable/delete/edit соответствуют CRON-01–07; tool создания можно запретить независимо от UI |
| 9. Guardrails | `core_agent/model.py`, `core_agent/tools.py`, `core_agent/runtime.py`, `core_agent/app.py` и UI решений | Проверка до использования/dispatch, карантин файлов, trusted-tool exemption, отказ/timeout/ошибка и продолжение без материала. Используются HITL/wait этапов 2–4; functional proof использует заданные verdicts, без benchmark качества на данных |
| 10. Очистка и удаление artifact tools | `core_agent/artifact_service.py`, `core_agent/runtime.py`, `core_agent/app.py`, `core_agent/config.py`, `pyproject.toml`, `uv.lock`, `.env.example` и UI файлов | Предпросмотр и ручная очистка не пересекаются с активной задачей. Три tools и исключительно их S3/Mongo/config/dependency код удалены после переноса входящих файлов; A2A результаты, transport artifacts и независимые snapshot flows сохраняются |
| 11. Сквозная поставка | `.github/workflows/ci.yml`, deployment/docs/config, `spec/implementation-status.md`, `spec/releases/v1.md` | Все ENT-AC сценарии связаны с проходящим CI proof; реальные PostgreSQL, Keycloak и Linux/Kubernetes проверки дополняют unit/E2E. UI содержит все согласованные настройки и состояния ошибок |

## Проверки реализации

Переиспользовать существующий `unittest` suite и helpers; не добавлять второй
test runner только ради новых требований. Основные существующие suites:
`tests/test_a2a_config.py`, `tests/test_end_to_end.py`,
`tests/test_postgres_persistence.py`, `tests/test_python_exec.py`,
`tests/test_tasks_tools_execution.py`, `tests/test_transfer_features.py`,
`tests/test_context_kernel_skills_security.py`,
`tests/test_runtime_observability.py`, `tests/test_memory_service.py`,
`tests/test_local_terminal.py`, `tests/test_compose_contract.py`.

Новые тестовые файлы добавляются по границам функциональности после разрешения,
без дублирования существующих проверок. Для каждого изменения сначала нужен
сценарий, который воспроизводит недостающее поведение; затем минимальная
реализация и targeted проверка. Runtime/config/tool gate:

```bash
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
```

Для persistence/HITL/races/recovery полный suite запускается с настоящим
`TEST_DATABASE_URL`; пропуск PostgreSQL тестов не является доказательством.
Для dependency/build changes дополнительно:

```bash
uv sync --frozen
uv build --no-sources
docker build -t core-agent:local .
```

Изменения образа, Compose и прав проверяются соответствующими smoke checks из
`.github/workflows/ci.yml`. Bubblewrap/egress проверяются на Linux и целевом
кластере; установленный `kubectl` сам по себе не доказывает наличие доступа к нему.

## PostgreSQL durability большого результата инструмента

- [x] Проследить actual tool dispatch → full artifact/excerpt → tool.completed
  commit → context replay; не вводить второй механизм сохранения результата.
- [x] Добавить ordinary CI regression в `tests/test_postgres_persistence.py`:
  fault после настоящего commit, закрытие app/pool, новый app/pool и resume_task.
  Проверяются exact Unicode/full result, bounded model excerpt, прежний pinned
  reference, transcript/blob/digest/provenance, чужой tenant и отсутствие redispatch.
- [x] Выполнить полный ordinary PostgreSQL/Keycloak CI: 1634 tests,
  260.004 секунды, exit0, три dedicated skips. Evidence —
  `.local-evidence/offload-postgres-ci-final/`. После read-only review уточнены
  assert pinned и early pool cleanup; targeted test повторён: 0.468 секунды,
  exit0 (`.local-evidence/offload-postgres-reviewed.log`). Product code не менялся.
- [x] Обновить CTX-03/release criterion и exact spec hashes после proof.
  Restart PostgreSQL daemon, OS kill и target CSI этим тестом не подтверждаются.

## Явный перенос remote configuration

- [x] Уточнить operator-only import в architecture/config/acceptance: version1
  bounded file, deployment tenant, empty registry, atomic encrypted batch,
  database migration actor, отказ повторного import без overwrite; schema24
  сохраняется, server startup и legacy ownership не изменяются.
- [x] Добавить regression в `tests/test_remote_registry.py` для actual CLI,
  PostgreSQL rollback при duplicate/key failure, nonempty registry после UI edit
  и malformed input без раскрытия values; выполнить red-phase существующим uv unittest.
- [x] Переиспользовать `PostgresRemoteRegistry.create` с borrowed transaction для
  batch import; добавить CLI/file parser в existing modules без новых dependencies.
- [x] Обновить README/AGENTS с operator workflow и выполнить targeted registry
  tests, Ruff и полный PostgreSQL/Keycloak suite; proof остаётся partial для
  ещё не реализованного identity mapping/production cutover.
- [x] Зафиксировать проверенный implementation commit и обновить evidence matrix.

Actual CLI red-phase подтвердил отсутствие команды. Review отдельно выявил
raw PostgreSQL diagnostic: real SQL error на втором peer воспроизводит canary
до safe wrapper; после исправления diagnostic скрыт и batch полностью откатывается.
Targeted suite — 52 tests, exit0; final ordinary PostgreSQL/Keycloak CI и
`uv build --no-sources` проходят. Evidence —
`.local-evidence/remote-import-ci-reviewed-final/` и
`.local-evidence/remote-import-review-green-final.log`.

## Готовность к поставке

Текущий срез включает owner UI, Keycloak scope, per-tool HITL/guardrails policy,
atomic chat admission/files, durable remote/time waits, same-chat cron,
семантическую суммаризацию и удаление трёх named artifact tools с exclusive
инфраструктурой. Старых named файлов нет; export/import исключён по явному
решению пользователя. Memory изолируется trusted tenant/app/user и namespace;
schema24 сохраняет legacy bytes под неизвестным tenant без догадок.

Проверенные границы и автоматические доказательства:

- Обычный Python3.12 CI на свежей БД/schema24 с настоящими PostgreSQL/Keycloak:
  `uv sync --frozen`, migrations, Ruff и `uv run python -m unittest discover
  -s tests -v` — 1634 tests, 260.004 секунды, exit0. Три dedicated skips
  относятся к native sandbox, actual browser и memory-loop ownership case;
  PostgreSQL/Keycloak tests не пропущены. Evidence —
  `.local-evidence/offload-postgres-ci-final/`; уточнённый test после review
  отдельно проходит. Предыдущие failed runs сохранены:
  remote-import CI выявил необходимость учесть новые CLI ENV в strict startup
  inventory; актуальный список исправлен без ослабления теста. Ранее
  тест summary пересекался с legitimate detector_busy из background tool.
  Для этого отдельного теста terminal помечен guardrails-exempt; production
  single-slot и owner decision при busy не изменены. 100 повторений проходят.
- Enterprise schema upgrade proof входит в ordinary discovery: source12 и
  каждый source13–23 переходят на текущую schema, сохраняют admitted state,
  wait deadlines/outcomes, настройки, encrypted peers и transport blob bytes
  после repeat migration/new pool. Old/unknown build отклоняется. Actual
  pg_dump/pg_restore schema23 и отдельная копия transport blobs восстанавливаются
  после удаления схемы и файлов; сохраняются deadlines, outcomes, scope и
  encrypted peer с прежним ключом. Проверены обратная schema version и повторный
  upgrade; в fixture нет новых application writes после backup. CI использует
  dump/restore utilities своего PostgreSQL service. Explicit remote config import
  проверяет atomic encrypted batch/database actor, отказ overwrite UI edits/disable,
  safe DB error и rollback; Task/chat identity mapping и production cutover
  остаются отдельными open gates.
- Combined release boundary module входит в ordinary discovery: cron после двух
  compactions, omitted summary vs authoritative policy/waits, оба remote
  input/auth-required bindings с follow-up после PG restart, manual/automatic
  admission race, active HITL после disable/delete schedule и private owner
  projection по Get/List/artifacts/SSE/encrypted push.
- Actual Chromium/Keycloak/PostgreSQL/native ARM64 Pod browser gate:
  1 test, 84.845 секунды, exit0, без skips. Проверены файлы/lost-ACK retry,
  HITL, per-tool policy, custom header secret editor, cron timezone/edit/manual
  run/delete, selected workspace cleanup и immutable history/download после
  удаления originals. Evidence — `.local-evidence/owner-browser-expanded-key/`.
- Полный required native ARM64 sandbox gate повторён на актуальном image:
  12 tests, 106.000 секунды, exit0, без skips; namespace/seccomp/rlimit/broker/
  teardown/dual-stack receivers, actual aggregate Pod pids.max512. Evidence —
  `.local-evidence/current-native-full/`; image ID закреплён в `image.json`.
- Docker Compose задаёт outer native seccomp, `/dev/net/tun`, bounded
  CPU/memory/PIDs, read-only root и dropped capabilities. Actual composition-root
  terminal/Python/background test через Compose — 1 test, 23.921 секунды,
  exit0; controlled model, настоящие launcher/processes, без privileged.
  Этот check включён в native CI. Evidence —
  `.local-evidence/docker-compose-sandbox-recovered/`.
- Native UI build stage позволяет собрать target amd64 на Mac без Node emulation.
  Образ schema24 с packaged UI/non-root user собран и загружен в закрытый
  registry; target node успешно скачал exact digest. Evidence —
  `.local-evidence/target-amd64-native-ui-build.*`,
  `.local-evidence/target-registry-push.*`,
  `.local-evidence/target-native-preflight/`.

Полный release gate пока открыт:

- В target Kubernetes профиль seccomp установлен по явному разрешению. Обычный
  Pod имеет finite PID limit9462: изменение kubelet не требуется. Kernel6.8
  отвергает user-namespace idmapped mount `/dev/net/tun` и `/dev/net`.
  TUN-device на отдельном временном CSI томе смонтирован через subPath без этой
  ошибки и без записи в Node filesystem; агент остаётся non-root/caps0.
  Actual app sandbox test не прошёл: стандартный AppArmor блокирует private
  mount propagation. Trusted diagnostic с AppArmor Unconfined разрешает mount,
  но Ubuntu unprivileged_userns ограничивает network capabilities; рабочий
  профиль не заменён таким обходом. Evidence — `.local-evidence/target-tun-csi/`,
  `.local-evidence/target-userns-diagnostics/` и
  `.local-evidence/target-userns-unconfined-diagnostic/`.
  Новый именованный профиль `core-agent-runtime-v1` подготовлен на основе
  containerd baseline с явными userns/mount/pivot_root rules. Syntax/compile
  AppArmor4.1.0, ABI4.0 и `--Werror=rule-not-enforced` проходят, exit0.
  Одноразовый установщик добавляет только этот профиль, сохраняет проверенные
  файлы в `/var/lib/core-agent/apparmor/` и не заменяет existing policies.
  Target server dry-run принят; временный namespace удалён. Kernel load не
  выполнялся: нужен отдельный ответ пользователя на запрос про MAC_ADMIN.
  Evidence — `.local-evidence/target-apparmor-install/`; это подготовка
  совместимости, не proof actual sandbox на target и не fallback Unconfined.
- Target Cloud.ru CSI byte persistence после удаления и пересоздания Pod
  подтверждён на exact amd64 digest, checksum совпадает, оба Pod non-root/caps0.
  Namespace и PV удалены. Это подтверждение тома, не сквозной ENT-AC-32:
  root/child/background/recovered commands после recreate ещё нужны вместе с
  actual amd64/target namespace/network checks. Evidence —
  `.local-evidence/target-csi-recreate/`.
- Реальный настроенный LLM возвращает HTTP404, Foundation Models embeddings
  endpoint — HTTP503. Model catalogue доступен, но live provider/embedding
  proof не прошёл. Контрактные tests не являются proof качества модели.
- Незавершённые foundation/migration release criteria остаются `partial`
  в `spec/implementation-status.md`; весь профиль не объявлен production-ready.
- Main обновляется после согласованных проверок; проверенные логические
  этапы сохранены в постоянной feature-ветке.
