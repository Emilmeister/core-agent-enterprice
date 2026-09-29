# Enterprise Core Agent — порядок реализации и план подготовки контрактов

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать согласованное ТЗ: общий UI владельцев, Keycloak, изолированные чаты и файлы, HITL, долгие A2A-операции, cron, guardrails и семантическую суммаризацию.

**Architecture:** Сохранить существующие A2A Task lifecycle, PostgreSQL workflow/inbox/outbox, leases и централизованный tool dispatch. Добавить авторизованный доступ владельцев и внешних агентов, постоянные папки чатов и Bubblewrap; ожидания сохранять в БД и освобождать worker.

**Tech Stack:** Python 3.12+, uv, существующие A2A SDK/ASGI, PostgreSQL/psycopg, Keycloak, Bubblewrap, Kubernetes. Стек UI выбирается в плане соответствующего этапа; отдельная БД и брокер сообщений для этих требований не вводятся.

---

Статус: подготовка реализации. Код продукта, `spec/**` и `tests/**` этим
изменением не затронуты. План первого этапа ниже касается нормативных
контрактов; таблица следующих этапов задаёт зависимости и проверяемый результат,
но не заменяет их подробные планы изменений кода.

Источник требований: [согласованное рабочее ТЗ](../specs/2026-09-29-enterprise-agent-design.md).
Правила работы: [AGENTS.md](../../../AGENTS.md) и
[spec-driven процесс](../../../spec/development-process.md).

## Условие начала изменений продукта

`AGENTS.md` требует отдельного явного разрешения на изменения `spec/**/*.md`,
`tests/**` и frozen hashes. Запрос начать реализацию не используется как
неявное снятие этого ограничения.

Нужно разрешение в рамках согласованного ТЗ:

- переносить требования в перечисленные ниже нормативные документы;
- добавлять и изменять проверки нового поведения в `tests/**`;
- обновлять `EXPECTED_SHA256` в `tests/test_spec_lock.py` после проверки spec diff,
  сохраняя саму проверку полного набора файлов и их точных bytes.

Не требуется повторное согласование уже принятых продуктовых решений.
Разрешение не означает удаление старых пользовательских данных, публикацию
сервиса или применение миграций к рабочей БД.

## Этап 0. Нормативные контракты и границы совместимости

### 0.1. Перенести согласованные решения в источник истины

- [ ] Получить указанное выше разрешение до любых правок замороженных файлов.
- [ ] Перенести требования по карте ниже, сохранив их смысл, значения по умолчанию
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

- [ ] Согласовать `spec/README.md` с новой картой capabilities. Не заменять
  authenticated identity полями в `RunRequest` и не создавать вторую публичную
  task state machine.
- [ ] Перенести все сценарии AC-01–AC-70, включая AC-17a, из рабочего ТЗ в
  `spec/acceptance.md`. Использовать префикс `ENT-AC-` для отличия от существующих
  критериев; сохранить таблицу соответствия исходным идентификаторам.
- [ ] Расширить scope текущего release candidate в `spec/releases/v1.md`.
  Новые обязательства оставить невыполненными до появления доказательств;
  удалить противоречия вида «HITL отсутствует» из целевого scope.
- [ ] В `spec/implementation-status.md` отделить новые обязательства от уже
  доказанного foundation behavior. Статус `implemented` не переносить на
  изменённую семантику по факту существования старого теста.

### 0.2. Зафиксировать совместимость до изменения соответствующего кода

- [ ] Описать переход со старого A2A входа на два авторизованных входа, mapping
  существующих identity/context и отсутствие автоматического присвоения старых
  данных новому внешнему caller. Номер публичного application contract отделить
  от версии A2A протокола.
- [ ] Определить версии persisted state и порядок миграции: chat ownership,
  создание задач/messageId, wait records, pending HITL, remote handles,
  расписания, file metadata и ссылки на содержимое. Миграция выполняется
  отдельной migration job; serving process не повышает schema version сам.
- [ ] Описать восстановление legacy workflow без повторного mutating dispatch.
  Старый checkpoint не интерпретировать как новую незапущенную операцию.
- [ ] Описать перенос `REMOTE_AGENTS` в управляемые владельцами настройки,
  отсутствие секретов в UI-ответах и сохранение старых файлов при удалении
  artifact service. До переноса загрузки файлов artifact backend не удалять.
- [ ] Для каждого изменения схемы указать допустимый порядок обновления image/БД
  и границу отката. Не обещать запуск старого image с неизвестной ему схемой.

### 0.3. Проверить и закрепить нормативный этап

- [ ] Просмотреть diff на соответствие рабочему ТЗ, в том числе различия между
  пробуждением `core_wait_until` и ожиданиями HITL/remote/ответа владельца.
- [ ] Обновить только утверждённые entries `EXPECTED_SHA256` по точным bytes
  нормативных файлов. Сохранить assertions и охват всех `spec/**/*.md`.
- [ ] Проверить ссылки, примеры и выполнить:

```bash
uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v
git diff --check
```

Ожидаемый результат: обе команды завершаются с exit code 0. Эти проверки
подтверждают согласованность документов, а не реализацию возможностей.

- [ ] Обновить `AGENTS.md` только в части изменившихся нормативных правил,
  отделяя их от фактически подключённых runtime capabilities.
- [ ] Зафиксировать проверенные изменения логическими spec commits, не смешивая
  их с несвязанными изменениями реализации.

## Порядок следующих этапов

Для каждого этапа сначала составляется подробный план по актуальному коду и
утверждённому нормативному контракту. Этап включает regression proof, подключение
в composition root и проверки; наличие отдельно написанного модуля не означает
завершение этапа. Выполнение — в текущей сессии, без автоматического делегирования.

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

## Готовность к поставке

- Нет новой анонимной production-границы и bypass через list/stream/download,
  Python broker, child task, memory, summary или восстановление.
- Подтверждение владельца, deadline, cancel и follow-up конкурируют через durable
  переходы; неоднозначные внешние side effects не повторяются автоматически.
- Данные и история не удаляются миграцией или удалением старого tool backend.
- `AGENTS.md`, конфигурация и инструкции развёртывания отражают фактический код.
- Изменённые требования получают `implemented` только после соответствующего
  автоматического доказательства в проходящем обычном CI suite.
