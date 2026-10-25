# Core Agent: завершение адаптации main для Cloud.ru AI Agents

> **For agentic workers:** После согласования использовать `subagent-driven-development` для независимых проверок или `executing-plans` для последовательных этапов. План согласован пользователем; связанные spec/tests/frozen hashes разрешены. Выполнение пакетное: весь код и документация, затем общий прогон и один candidate image. Документ не утверждает готовность выпуска.

**Goal:** Довести `feat/cloudru-ai-agents` до рабочей поставки возможностей текущего `main` на Cloud.ru AI Agents, сохранив согласованные отличия платформенного профиля.

**Architecture:** Общие runtime, инструменты, UI, PostgreSQL и основной A2A 1.0 остаются едиными с main. Cloud.ru использует платформенный ingress, пароль владельца, совместимый внешний A2A 0.3, Landlock/seccomp и S3 для файлов; управление кластером не требуется.

**Tech Stack:** Python/uv, PostgreSQL, S3, Landlock/seccomp, официальный A2A SDK, React/TypeScript, существующие unittest и native/browser проверки.

**Статус:** базовый пакет реализован 24 октября 2026; 25 октября выполнен live-прогон и опубликованы исправления отмены. После rollout `cloudru-s3-cancel-20261025` отмена работающей background-задачи с S3 подтверждена в Cloud. Рекурсивное discovery собственной Agent Card исправлено в пакете `cloudru-card-discovery-20261025`; после его rollout реальный исходящий Send/Get/Cancel также прошёл в Cloud. Остальная матрица и release proofs ещё не завершены (результаты ниже).

Общий ordinary suite: 2404 tests, exit 0, 16 платформенных skips; PostgreSQL,
Keycloak и MinIO работали в изолированных fixtures. В linux/amd64 candidate
прошли 178 config/memory/S3 tests без skips. ARM64 Landlock VM: 39 tests без
skips после повторного запуска; первая ошибка preflight не воспроизвелась,
причина не установлена, добавлена конечная приватная диагностика.
Ruff, spec lock/quality, UI tests/typecheck/build и wheel/sdist/image build прошли.
Это не подтверждение native amd64/arm64 CI и полного target Cloud release gate.

## 1. Исходная точка

Проверено по Git: `main@6434a40` целиком входит в `feat/cloudru-ai-agents@fba6edd`.
Отсутствующих коммитов main нет: результат `git rev-list --left-right --count main...feat/cloudru-ai-agents` — `0 18`.
Повторный перенос runtime, памяти, cron, MCP, skills или UI не нужен.
Перед выпуском повторяем сравнение, чтобы учесть появившиеся за время работ изменения main.

Уже реализованы и проверены в целевом Cloud-развёртывании:

| Возможность | Что подтверждено |
|---|---|
| Запуск приложения | PostgreSQL, Landlock, Python, терминал, HTTP(S), остановка команды по таймауту |
| Доступ | Вход владельца через UI; внешний чат Cloud.ru отвечает через адаптер A2A 0.3 |
| Файлы | Создание, изменение, перенос, удаление и восстановление рабочего файла после обновления Pod |
| История | Открывается после потери локального cache; устранён `STORAGE_NOT_PREFETCHED` |
| Входящие оригиналы | Скачиваются через API и настоящие кнопки браузера после удаления рабочей копии; совпадает SHA-256 |
| Результаты | Опубликованный файл сохраняет исходные байты после правок рабочей копии и обновления Pod |
| Взаимодействия | Проверены отдельные сценарии вопроса владельцу, отмены и отказа от вложения |
| Последнее исправление | 99 целевых Linux/PostgreSQL/MinIO проверок без пропусков; UI tests/typecheck/build |

Образ на исходной точке: `blockchain.cr.cloud.ru/core-agent-gn:incoming-originals-20261024`;
digest `sha256:539c38fa73bd38ba14331f09f916bb29bea2c991e8ccf0a189864835ae824f7b`.
Эти результаты закрывают перечисленные сценарии, но не полный набор возможностей main.

## 2. Согласованная граница готовности

Цель следующего этапа — поддерживаемый Cloud.ru-профиль для постоянной работы.
До этого пакета код разрешал Cloud.ru + S3 + Landlock только в `development`.
Согласовано реализовать поддержку этого состава в `production`,
с обязательными PostgreSQL/S3, стабильным ключом шифрования, проверенным sandbox
и отдельным запуском миграций. Простое удаление проверок `development` не является задачей.

Согласованные отличия от Kubernetes/main сохраняются:

| Область | Решение Cloud.ru | Что учитываем при приёмке |
|---|---|---|
| Владельцы | Общий пароль и сессии в PostgreSQL | Нет персональной Keycloak identity каждого владельца |
| Внешние агенты | Ключи выдаёт Cloud.ru; приложение использует общий внешний scope | Разные платформенные ключи не создают изоляцию peers внутри приложения |
| Исполнение | Landlock/seccomp, без новых namespaces | Это отдельный профиль изоляции со своими проверяемыми гарантиями |
| Сеть команд | HTTP(S) через проверяющий proxy | Произвольные TCP/UDP и клиенты без proxy не обещаются |
| Пути и загрузки | Реальный рабочий путь; изменяемые `inputs/` и отдельные неизменяемые оригиналы | Не обещается тождественность mount layout и syscall-поведения Bubblewrap |
| Фоновая работа | Первый поддерживаемый режим — один постоянно работающий экземпляр | Проверяем CPU вне HTTP-запросов, отсутствие scale-to-zero и краткое перекрытие экземпляров при rollout |

Если платформа не позволяет обеспечить постоянное выполнение или обязательную
изоляцию, это отдельный блокер готовности с конкретным решением на согласование.
Успешный чат не заменяет эти гарантии.

## 3. Оставшаяся работа — по порядку

| Этап | Тип работы | Что сделать | Условие завершения |
|---|---|---|---|
| 1. Закрыть конфигурационный дефект памяти | Обязательный код | При включённой памяти Cloud.ru требовать PostgreSQL в общей точке создания backend; исключить молчаливую потерю памяти из-за default `in-memory` | Неподходящая конфигурация отклоняется до serving; отключённая память допустима; PostgreSQL backend запускается |
| 2. Сделать Landlock обязательной проверкой | Обязательный CI-код | Подключить существующие native Landlock/seccomp тесты к CI с required flags; проверить выбранную платформу образа | Отсутствие enforcement приводит к ошибке gate, а не skip; сохраняется проверка Bubblewrap-профиля main |
| 3. Подтвердить все основные возможности main в Cloud | Проверки; код только по обнаруженным дефектам | Пройти матрицу ниже с реальными PostgreSQL/S3, моделью, MCP и контролируемым A2A peer | Для каждого пункта есть воспроизводимый результат, ошибки исправлены в общей точке |
| 4. Подтвердить recovery и фоновую работу | Проверки; код по обнаруженным дефектам | Idle, background, cron, ожидания, активная команда при остановке, замена Pod и rolling overlap | Нет потерянных подтверждённых данных, повторных side effects и одновременно действующих владельцев lease |
| 5. Довести эксплуатацию | Проверки и необходимые конфигурационные исправления | Миграции, runtime role, секреты/ротация, backup/restore, storage outages/GC/лимиты, telemetry | Обновление и восстановление воспроизводимы; ошибки не маскируются как успех |
| 6. Завершить production-профиль | Код и связанные контракты после этапов 1–5 | Согласованно обновить startup gates для Cloud/S3/Landlock, требования к конфигурации, spec/tests/hash lock | Валидный production-состав запускается; неполный или неподдержанный состав закрыто отклоняется |
| 7. Общая регрессия и выпуск | Проверки и документация | Полный CI, проверка main/Keycloak и Cloud, immutable tag/digest, инструкция обновления/отката, актуальная таблица отличий | Есть единый проверенный образ и список подтверждённых гарантий/ограничений |

### Этап 1. Память: подтверждённый дефект

`spec/cloudru.md` требует PostgreSQL при включённой памяти.
Сейчас `core_agent/app.py::_memory_registry` по умолчанию выбирает `in-memory`,
а запрет этого backend действует только в `production`. Работающий Cloud trial
использует `development`, поэтому ограничение обходится обычной конфигурацией.

Файлы: `core_agent/app.py`; существующие Cloud configuration и memory tests.

- [x] Добавить регрессию для включённой памяти Cloud с отсутствующим/неверным backend, корректного PostgreSQL и полностью отключённой памяти.
- [x] В общей точке startup потребовать поддерживаемый persistent backend без изменения поведения Keycloak/development вне Cloud.
- [ ] Проверить реальную запись, чтение, поиск, embeddings/NER и сохранение после замены Pod.
- [ ] Сохранить изменение отдельным коммитом с соответствующим proof.

### Этап 2. Landlock: CI и целевая среда

Файлы: `.github/workflows/ci.yml`, `deploy/landlock-native-vm.sh` при необходимости;
существующие `tests/test_landlock_policy.py`, `test_landlock_network.py`,
`test_landlock_metadata.py`, `test_sandbox_landlock.py`, `test_landlock_chat_files.py`.

- [x] Выбрать native Linux fixture для поставляемого `linux/amd64` образа с требуемым Landlock ABI; переиспользовать существующие tests и tooling.
- [x] Сделать обязательными `CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1` и `CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1` в этом gate.
- [ ] Проверить недоступность чужих workspace и секретов, запрет прямого/private/metadata egress, IPv4/IPv6, broker isolation и завершение дерева процессов.
- [ ] Отдельно подтвердить применимые свойства в целевом Cloud Pod контролируемыми ограниченными пробами.

Команда существующего набора внутри подготовленной native fixture:

```sh
CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1 CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1 \
uv run python -m unittest tests.test_landlock_policy tests.test_landlock_network \
  tests.test_landlock_metadata tests.test_sandbox_landlock tests.test_landlock_chat_files -v
```

Локальная ARM64 VM — дополнительный fixture, не замена amd64 и целевого Cloud gate.

### Этап 3. Матрица функционального соответствия main

Здесь проверяем уже существующую реализацию. Новая подсистема появляется только
при доказанной необходимости; обычный дефект исправляется в существующем общем коде.

| Группа | Сценарии, которые ещё нужно закрыть полностью |
|---|---|
| Модель и контекст | Обычные/streaming ответы, tools, reasoning settings, ограничение бюджета, compaction длинного контекста, сохранение настроек после restart |
| Память | CRUD, hybrid search, фактические embeddings/NER, shared memory по разрешениям, restart без потери данных |
| Skills | Каталог, активация и чтение ресурсов; ограничения allowlist у root и child |
| MCP | Настройка через owner UI, discovery/call, сохранение credentials, policy/HITL/deny, восстановление подключения после restart |
| Локальные агенты | `core_delegate`, background Task, wait/message/cancel, совместная папка своего чата, отдельные процессы и запрет доступа в соседний чат |
| Python/terminal | Nested `tools.call`, файлы, timeout/cancel, orphan cleanup, длительная фоновая команда, понятные ошибки неподдержанного поведения Landlock |
| Cron и ожидания | Запуск по расписанию, время/часовой пояс, owner question/HITL wait, remote wait, работа без открытого UI |
| Внешние A2A peers | Исходящие Send/Get/Cancel, повторные обращения с сохранением peer context, входящие/исходящие файлы, peer history и push там, где включён |
| Входящие A2A | Чистые карточки 0.3/1.0, built-in Cloud chat, root/external alias, owner 1.0, SSE reconnect, follow-up и dedup одного Message между входами |
| Owner UI и доступ | Настройки, история/материалы/peer panel, login/logout, expiry/rotation сессий, CSRF/Origin, отсутствие owner authority у внешнего входа |

Основные файлы уже существуют: `runtime.py`, `model.py`, `context.py`, `memory*.py`,
`mcp.py`, `skills.py`, `cron*.py`, `remote_agents.py`, `remote_operations.py`,
`a2a_sdk.py`, `a2a_compat.py`, `cloudru_auth.py`, `ui/src/`.
Результат этапа — заполненная матрица «прошло / дефект / ограничение профиля»,
а не повторная реализация перечисленных функций.

### Этап 4. Recovery, параллельность и жизненный цикл Pod

Файлы проверки/возможных исправлений: `execution_identity.py`,
`execution_reconciliation.py`, `runtime.py`, `workflow.py`, `postgres_tasks.py`,
`postgres_workspace.py`, `workspace_durability.py`, `cron_service.py`.

- [ ] Два независимых чата одновременно: свои файлы, Task, результаты и команды; соседние данные недоступны.
- [ ] Root/child в одном чате: правильная координация workspace, checkpoint и отмены.
- [ ] Закрытый браузер и длительный idle: cron, фоновые задачи и durable waits продолжают обслуживаться.
- [ ] SIGTERM/замена Pod при активной команде: команда не запускается повторно; подтверждённая остановка либо честный `SIDE_EFFECT_UNKNOWN` с действующим barrier.
- [ ] Замена Pod во время owner/HITL/time/remote wait: прежнее ожидание, deadline и ответ восстанавливаются без повторных side effects.
- [ ] Краткое перекрытие старого и нового экземпляров: общие owner sessions, один cron leader, fencing workspace writer и execution generation.
- [ ] Повторный холодный доступ к большим/смешанным вложениям, итоговым и peer artifacts; S3 I/O не удерживает PostgreSQL transaction.

Использовать отдельные тестовые чаты и контролируемые побочные эффекты.
Проверка сохранности файла после restart уже выполнена; повторяем её здесь
только как часть нового сценария активной задачи, overlap или storage outage.

### Этап 5. Эксплуатация и данные

- [ ] Проверить отдельную миграцию схемы и права runtime role на disposable копии; сохранить обязательный запрет production auto-migration.
- [ ] Проверить восстановление согласованного состояния PostgreSQL + referenced S3 objects со стабильным encryption key.
- [ ] Описать откат образа/схемы: старый backup нельзя накатывать поверх новых записей без отдельной процедуры сохранения данных.
- [ ] Проверить недоступность/таймаут S3, заполнение локального cache, ограничения размера/числа файлов и GC без удаления referenced originals/artifacts.
- [ ] Проверить согласованную ротацию owner password и отзыв платформенного ключа на уровне Cloud ingress.
- [ ] Проверить экспорт traces/metrics/logs. Исправлять прежний `403 Forbidden` только после подтверждения, что он остаётся на текущей конфигурации.
- [ ] Проверить readiness/liveness и записать фактические настройки ресурсов/фонового исполнения, доступные в AI Agents.

Использовать существующие `core-agent-db`, S3 ledger/GC и тесты миграций;
новый backup-сервис или новую HA-архитектуру заранее не добавлять.

### Этап 6. Завершённый Cloud production-профиль

Production configuration реализуется в общем пакете до объединённого прогона.
Допуск code path не означает release approval: реальные предпосылки Landlock,
фоновой работы и ограничения раздела 2 проверяются отдельно на одном candidate.

Файлы: `core_agent/auth.py`, `core_agent/app.py`, `core_agent/sandbox_landlock.py`;
связанные `spec/cloudru.md`, `spec/releases/v1.md`, `spec/acceptance.md`,
`spec/security-and-reliability.md`, Cloud config tests и `tests/test_spec_lock.py`.

- [x] Согласованно задать поддерживаемое сочетание Cloud auth + PostgreSQL + S3 + Landlock для production.
- [ ] Обязать стабильные secrets, правильные storage roots, schema/runtime role и проверенный sandbox до readiness.
- [x] Сохранить отказ при ephemeral storage, недоступном sandbox/БД, неверных credentials/settings и неподдерживаемой комбинации режимов.
- [ ] Проверить прежний Keycloak/Bubblewrap профиль main и отсутствие автоматического переключения профилей при ошибках.
- [x] Удалить устаревшие сообщения о том, что S3 ещё не реализован; диагностика должна отражать реальную причину отказа.

### Этап 7. Общая регрессия и выпуск

- [x] Вновь сверить ancestry main и Cloud-ветки; новые main-исправления включать осознанно и проверять совместимость с адаптацией.
- [ ] Поднять существующие disposable PostgreSQL, Keycloak, MinIO и native/browser fixtures согласно CI.
- [ ] Выполнить полный ordinary suite, все затронутые native/security/browser gates и packaging/image checks.
- [ ] Зафиксировать proof по каждому пункту матрицы; обновить implementation status только там, где доказательство действительно получено.
- [x] Обновить `AGENTS.md`, `docs/cloudru-trial.md` либо его назначение для завершённого профиля, ENV-инструкцию и документ сравнения Kubernetes/Cloud.ru.
- [ ] Опубликовать проверенный образ под постоянным version tag и digest, затем проверить rollout и доступность на целевом endpoint.

Базовые команды существующих gates:

```sh
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
```

Из `ui/` с Node.js 24:

```sh
npm ci
npm test
npm run typecheck
npm run build
```

Эти команды требуют настроенных fixtures; отсутствующая инфраструктура или
skip обязательного native gate не считаются подтверждением готовности.

## 4. Согласованный пакетный порядок

1. Собрать все необходимые изменения кода, CI, spec/acceptance, regression tests,
   frozen hashes и эксплуатационных документов этапов 1–2/5–6 в рабочей ветке.
2. Выполнить один объединённый набор затронутых и обязательных regression/native/
   browser/package gates. Исправления выявленных дефектов включить в этот пакет;
   повторять только проверки, необходимые после изменений или failures.
3. Собрать и опубликовать один candidate image с постоянным tag/digest. Передать
   единый rollout checklist; не обновлять Cloud deployment после каждой правки.
4. Проверить этапы 3–5/7 на этом candidate. Существующий Cloud development
   deployment сохраняет tenant, DB/S3 references и secrets; production switch
   требует подготовленной отдельной migration и стабильного encryption key.
   До этих условий candidate проверяется с действующим development profile.
5. Зафиксировать реальные proofs и оставшиеся blockers. Code/CI support
   production не объявлять завершённым release без target idle/security/recovery.

Рабочая ветка остаётся `feat/cloudru-ai-agents`. Пользователь разрешил связанные
изменения spec, acceptance, tests и frozen hashes для согласованных доработок.
План не требует пересоздания проекта или изменения параметров кластера Cloud.ru.

## 5. Результаты общего live-прогона 24–25 октября 2026

Проверки выполнены на существующем Cloud deployment после подтверждённого
пользователем rollout. Ожидаемый candidate — `cloudru-batch-20261024`, index
`sha256:35609371e1aab984f4f68c987fe35e92a0d1059ec23da1900580399e615a6dcc`.
HTTP API не раскрывает revision/digest, поэтому точный запущенный image независимо
не подтверждён. Это manual deployment proof, не основание менять CI implementation status.

| Область | Полученное доказательство | Остаток / ограничение |
|---|---|---|
| Входящий A2A и доступ | 35 проверок: карточки 0.3/1.0, root/alias dedup, owner-only маршруты, Origin, запрет перечисления external Tasks; обе тестовые задачи завершились точными ответами | SSE reconnect, rotation/expiry и rolling overlap проверяются отдельно |
| Owner UI | Настоящий Chromium: вход, чтение своей истории, отображение результата, история после reload, отсутствие JS errors, logout → identity 401 | Не заменяет обязательный native browser/Keycloak gate |
| Cron и время | Создание, повтор запроса, ручной запуск без дублирования, CAS, disable/delete; новый Message прерывает timer, повтор Message не создаёт новый запуск | Автоматическое расписание при длительном idle ещё не доказано |
| Idle | После подтверждённого закрытия UI — 300 секунд без наших HTTP-запросов. Timer проснулся в 19:57:30.834592 UTC; первое чтение после паузы в 19:58:15 UTC уже увидело COMPLETED | Доказывает один пятиминутный интервал; не гарантия отсутствия scale-to-zero или рестартов платформы |
| Память и child | Create/read/update rev1→2, точный vector result, child с allowlist search/read прочитал ту же запись; delete committed и последующий поиск пуст | NER вернул degraded graph «1 document(s) not extracted». Причина требует безопасного provider error code из журнала; restart памяти отдельно |
| Skills и исполнение | Активация skill с digest, Python `tools.call` → terminal, отсутствие пяти проверенных secret ENV, отказ чтения своего canary из соседнего чата с EACCES, timeout → cleanup → следующая команда | Не доказывает весь сетевой профиль, resource limits или остановку при смерти Pod |
| Background cancel | Создание фоновой команды и mailbox message подтверждены | После разрешённого cancel root завершился SIDE_EFFECT_UNKNOWN. Публичных данных недостаточно для определения cleanup/lease причины; повторный запуск команды не выполнялся |
| Исходящий peer | Карточка self-peer совместима с pinned A2A 1.0 alias | Сохранение подключения с ключом вернуло 503 REMOTE_AGENT_CREDENTIAL_UNAVAILABLE. Подключение/задача не созданы; требуется настроенный стабильный encryption secret |

Исходные безопасные receipts находятся рядом с worktree в
`.enterprise-work/cloudru-batch-live-*-20261024.json`: protocol, control-v2,
tools, memory-child, browser-v2, idle и peer. Первые control/browser попытки
содержат ошибки тестовой подготовки (несуществующий chat ID, чтение timer через
owner interactions, строковый JS eval при CSP); они не считаются product defects.
Сетевые ReadTimeout/ConnectTimeout сохранены как наблюдения без догадки о причине.
Все собственные model roots завершены либо отменены; тестовые memory записи,
canary-файлы и временное расписание удалены. Общие policies/settings не менялись.

### Пакет исправлений по итогам прогона

Отдельно подтверждены два дефекта общей реализации отмены: безопасный отказ
до dispatch ошибочно превращался в unknown; cleanup wrappers теряли конечную
приватную SandboxDiagnostics. Исправление сохраняет прежние public error
codes, отличает pre-dispatch от callback после cancel и оставляет uncertain
side effects закрытыми. В частности, `TOOL_ARGUMENT_INVALID`/`TOOL_START_FAILED`
после dispatch не разрешают продолжить model loop. Оно не устанавливает причину
упомянутого live incident.
Дополнительная ARM64 native regression с реальным child, TerminalSessionManager
и concurrent reaper прошла; исходный Cloud сбой в ней не воспроизведён.
На итоговом коде прошли 205 targeted tests с отдельной PostgreSQL БД и 383 tests
в собранном linux/amd64 candidate с PostgreSQL/MinIO, без skips; независимое
повторное review блокеров не выявило. Это не заменяет native sandbox gates.
Общий ordinary suite на итоговом коде: 2410 tests, exit 0, 16 платформенных skips,
с настоящими PostgreSQL, Keycloak и MinIO. Ruff, frozen spec lock и wheel/sdist
build прошли. Пропуски не засчитываются как прохождение соответствующих gates.

Следующий rollout объединяет исправления и настройку encryption secret.
После него повторяются failed/blocked случаи: active background cancel с точной
диагностикой, peer credentials + Send/Get/Cancel, provider NER. Для MCP нужен
контролируемый доступный HTTPS fixture; для compaction — provider + две реальные
compaction receipts; для production — runtime DB grants, coordinated backup,
native amd64/arm64 gates и один согласованный restart/overlap сценарий.

### Повторный Cloud-прогон 25 октября и причина S3 cancel race

После rollout `cloudru-batch-fixes-20261025` безопасный отказ отмены до dispatch
подтверждён в Cloud: `TASK_NOT_FOUND` сохранился в tool receipt, следующая
terminal-команда выполнилась, root завершился `COMPLETED`.
Отмена работающего background `sleep` повторно дала `SIDE_EFFECT_UNKNOWN`.
В предоставленных оператором логах перед этим виден `LEASE_LOST` при checkpoint
child; доказательств timeout остановки процесса в этих строках нет.

Локальная детерминированная проверка с PostgreSQL выявила расхождение общего
workflow store и S3 workspace coordinator: `confirm_execution` сохраняет
подтверждение остановки без изменения версии run, lease token или generation.
S3 guard сравнивал весь прежний `execution_owner` с новым receipt и отвергал
checkpoint известного результата как `LEASE_LOST`. Обычный workflow transition
с теми же record/token принимает результат и сохраняет актуальный receipt.
Это воспроизведение дефекта компонента; приватное состояние live lease из
публичного API не считывалось.

После отдельного добавления стабильного `PUSH_NOTIFICATION_ENCRYPTION_KEY`
обе health-проверки вернули 200. Создание собственного тестового peer с
API-ключом прошло, `has_header_value=true`: прежний configuration blocker
`REMOTE_AGENT_CREDENTIAL_UNAVAILABLE` снят. Функциональные Send/Get/Cancel
пока не подтверждены: собственный root оставался `WORKING` 610 секунд без
tool receipts и без исходящих операций. Один cancel-запрос завершился
`ReadTimeout`; последующее чтение подтвердило `CANCELED`, запрос не повторялся.
Собственное подключение удалено и отсутствие проверено, чужие настройки не
менялись. Причина остановки продвижения root требует deployment logs, это ещё
не доказательство ошибки исходящего A2A transport.
Первая отправка не задавала `returnImmediately` и использовала blocking default
SDK; HTTP timeout сам по себе не доказывает сбой admission. Повтор с явным
`configuration.returnImmediately=true` получил Task за 1,35 секунды, но снова
не дал tool/outgoing receipts за 312 секунд. Собственный root затем подтверждён
как `CANCELED`, собственный peer удалён с readback. Простой отдельный запрос без
инструментов вернул точный контрольный ответ за 101,63 секунды от начала пробы;
его timed-out blocking Send не повторяли, результат нашли по исходному context.

Безопасные receipts: `.enterprise-work/cloudru-batch-live-cancel-20261025.json`,
`cloudru-cancel-ownership-repro-20261025.json` и
`cloudru-batch-live-peer-key-20261025.json` рядом с worktree.
Следующий пакет исправляет совместимость checkpoint с монотонным receipt
подтверждённой остановки; tenant/scope/version/live lease/generation проверки и
`ABORTED` для неизвестного результата orphan execution сохраняются.
Целевое воспроизведение исправления: 14 тестов без skips, включая реальный
процесс с PostgreSQL/MinIO, сохранённый результат отмены и восстановление файла
после потери локальной рабочей копии. Независимое review блокеров не выявило.
В собранном linux/amd64 candidate прошли 409 тестов без skips. Полный ordinary
suite: 2414 тестов, exit 0, 16 платформенных skips с отдельными PostgreSQL,
Keycloak и MinIO. Первый общий прогон выявил гонку прежнего timer-теста:
assertion о `working` выполнялся до запуска child thread. Детерминированное
воспроизведение подтвердило этот порядок; тест теперь ждёт `started` Event,
сохраняя проверки отмены и завершения. Повторный полный прогон прошёл.
Ruff, frozen spec lock, diff check и wheel/sdist build прошли. Native release
gates остаются отдельными; результат повторной отмены после rollout приведён ниже.

Опубликован один новый candidate под тегами
`blockchain.cr.cloud.ru/core-agent-gn:cloudru-s3-cancel-20261025` и `latest`:
index `sha256:ff6031cd5400bffb86df1389f265f2563fad04a8254bf11c302a6dda85aafa3a`.
Readback registry подтвердил одинаковый index обоих тегов и совпадение runtime
amd64 manifest/config с проверенным локальным образом. Общие результаты и
digests сохранены в `.enterprise-work/cloudru-s3-cancel-20261025.json` рядом
с worktree; дополнительные live receipts — `cloudru-batch-live-peer-key-v2-20261025.json`
и `cloudru-batch-live-model-control-20261025.json`. Собственные regression fixtures
остановлены; существующие пользовательские контейнеры не менялись.

Дополнительные NER/summary probes использовали другой локально настроенный
DeepSeek endpoint, а не Cloud Qwen. Четыре вызова не получили HTTP-статуса;
отдельно обнаружено отсутствие CA в локальном Python default TLS context.
Эти попытки не подтверждают несовместимость модели и не засчитываются как
проверка deployed NER/compaction; обход TLS не применялся.

### Повтор после rollout S3 cancel и следующий пакет Card discovery

После подтверждённого пользователем rollout проверены readiness и owner login.
Новый root `128059a8-c9dd-4231-a325-cd76384a9cbf` принят с явным
`configuration.returnImmediately=true`. Единственный background `sleep 300`
`76ba5deb-9c27-5fa4-ab2a-ad23e7412412` имел `working` в реальном `core_task_get`
до отмены и `canceled` в последующем `core_task_wait`. Все шесть tool receipts
успешны; после отмены terminal вернул точный marker, exit 0 и
`cleanup=sandbox_terminated`, root — `TASK_STATE_COMPLETED`.
Проверены persisted receipts, не только текст модели; прямого доступа к OS
handles child и digest работающего Pod нет. Новых политик, peers и файлов этот
тест не создавал. Отчёт: `cloudru-live-s3-cancel-20261025-v2.json` рядом с worktree.

Локальное ограниченное воспроизведение отдельно выявило цикл исходящего A2A:
при чтении собственной Agent Card `card_skills` вызывал `_remote_peers`, а
discovery self-peer повторно обращался к той же Card до model request.
Нормативное исправление разделяет metadata capability Card и фактическую
доступность адресатов в model catalog/dispatch; wire versions, credential checks
при вызове и tenant boundaries сохраняются. Это доказанный локальный дефект,
но private logs прежних Cloud зависаний пока не подтверждают, что там был
именно этот путь. Результат проверки после отдельного rollout приведён ниже.

Regression proof: три новых теста сначала воспроизвели нарушение, затем прошли.
Проверены self-peer без вложенного discovery, сохранение настоящего runtime
discovery, обе карточки owner/external, tenant/policy/platform ceiling,
disabled/deleted peers и enabled запись после первой страницы. Карточки не
читают credentials; ранее принятые reply bindings остаются доступны runtime.
Независимое review блокеров не выявило.

Полный ordinary suite: 2417 тестов, exit 0, 16 dedicated native/browser skips,
563,189 секунды с реальными PostgreSQL 17, Keycloak и MinIO. В собранном
linux/amd64 candidate прошёл 251 remote runtime/auth/Cloud/transport/registry/
operations/progress/spec-lock тест без skips на отдельной тестовой БД и bucket.
Ruff, шесть spec lock/quality проверок, diff check, wheel/sdist и image build
прошли. Публикация и digests candidate фиксируются в
`cloudru-card-discovery-20261025.json` рядом с worktree; полный лог —
`cloudru-batch4-full-suite-20261025.log`. Эти проверки не заменяют native release
gates; фактический исходящий Send/Get/Cancel проверен отдельно после rollout.

### Cloud Send/Get/Cancel после rollout Card discovery

25 октября, 19:06–19:10 UTC: readiness и карточка вернули 200, собственный peer
с API-ключом сохранился. Root `0019261a-23ef-48d4-960e-c769b9b9e341` принят за
0,64 секунды с `returnImmediately=true`. Единственный реальный
`core_agent_send_message` создал operation `d1e4516d-a646-5006-ae05-800cfc654865`;
её `request_delivery=confirmed`. Пока root ждал точный ответ владельца и до
отмены, `last_checked_at` той же операции вырос с 1792955303.982321 до
1792955347.295846: подтверждён отдельный worker Get, а не только локальное
чтение scheduler snapshot.

После ответа владельца выполнены по одному `core_task_cancel` и `core_task_wait`.
Четыре tool receipts успешны, root — `TASK_STATE_COMPLETED`, operation —
`canceled`, remote — `TASK_STATE_CANCELED`, `outcome_unknown=false`, ошибок нет.
Собственное подключение удалено; registry readback подтвердил его отсутствие.
Повторных model Send/Cancel и повторной отправки root не было, общие настройки
и чужие подключения не менялись. Report со всеми receipts:
`cloudru-batch-live-peer-key-v3-20261025.json` рядом с worktree.

Это proof протокольного Send/Get/Cancel через реальный ingress. Получатель
находился в owner wait; запуск его `core_wait_until` и отмена активного таймера
этой проверкой не доказаны. Live image digest не доступен через публичный API;
проверка выполнена после подтверждённого пользователем rollout.

### Пакет MCP и NER 25 октября

| Проверка | Фактический результат | Граница доказательства |
|---|---|---|
| MCP в Cloud | Owner settings приняли отдельный временный сервер Microsoft Learn; единственный `microsoft_docs_search` вернул успешный receipt, root завершился `COMPLETED`. Собственные input/HITL approvals применены к точным материалу и вызову. | Реальный discovery/call через Cloud, без credentials на публичном сервере; controlled auth/negative/SSE сценарии в Cloud этим не проверены. |
| Очистка MCP | CAS-обновление удалило только собственное подключение; readback подтвердил пустой исходный список и восстановленное наследование. Другие поля settings не записывались. | Одно подключение, без конкурентной смены владельцем того же списка. |
| NER и hybrid search в Cloud | Собственная session-заметка с полностью строчными title/body найдена по `postgresql`: `graph=1`, `vector=0.4601315122803613`, `degraded_channels=[]`. Начальный поиск пустой. | Встроенный regex извлекает только слова с заглавной буквы: этот результат подтверждает model-derived grounded entity по текущему code path. Прямого provider trace и оценки качества остальных сущностей нет. |
| Очистка памяти | Ровно шесть успешных receipts: search × 3, create/read/delete × 1. Удалён только созданный `memory_id` с revision 1; `committed=true`, повторный поиск пустой, root `COMPLETED`. | Подтверждено логическое удаление своей заметки; физическое удаление истории и сохранность NER после замены Pod не проверялись. |
| Локальная регрессия | MCP: 71 тест и ещё 2 PostgreSQL проверки; память: 59 тестов с PostgreSQL; контекст: 50 тестов. Все итоговые прогоны завершились с exit 0 без skips. | Это targeted proofs; они не заменяют полный native/browser release gate. |

MCP root: `22a87d39-cec7-4f22-bd18-02673d16adfc`; NER root:
`9b5515cd-dbc3-4971-b181-84435506ea11`. Безопасные отчёты рядом с worktree:
`cloudru-mcp-review-20261025.json`, `cloudru-mcp-live-20261025.json`,
`cloudru-ner-live-20261025.json`; memory log:
`cloudru-memory-ner-local-20261025-retry.log`.
Первый PostgreSQL прогон после старта fixture использовал устаревший динамический
порт и упал; повтор с портом из Docker readback прошёл. Пользовательские
контейнеры и deployment environment не менялись; собственный fixture остановлен.

Отдельная изолированная попытка summary с локально настроенным DeepSeek и
проверенным CA дошла до HTTP 404 за 3,068 секунды. После первого отказа
остановлена: один запрос, ноль compactions, без повторов и без изменений Cloud.
Отчёт `cloudru-batch-compaction-provider-20261025.json` не является проверкой
deployed Qwen и не устанавливает дефект алгоритма сжатия.

## 6. Когда цель считается достигнутой

Все основные возможности текущего main проходят согласованную матрицу на
Cloud.ru; новые Cloud storage/auth/sandbox границы имеют обязательные proofs.
Данные и ожидающие задачи переживают обновление в пределах заявленного
контракта, активные side effects не повторяются молча. Есть проверенный
production-профиль, воспроизводимое обновление/восстановление, фиксированный
образ и понятный список отличий от Kubernetes/main.
