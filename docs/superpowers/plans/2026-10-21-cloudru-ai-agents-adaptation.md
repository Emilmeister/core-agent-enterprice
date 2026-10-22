# План адаптации к Cloud.ru Evolution AI Agents

Дата: 21 октября 2026 года. Исходный enterprise commit: `6434a40`.
Ветка реализации: `feat/cloudru-ai-agents`.

Статус на 22 октября: минимальный Landlock trial запущен пользователем.
Следующий согласованный этап — код S3 workspace/response storage, восстановления,
понятных относительных file paths и trusted времени модели. Этап расширен:
incoming originals сохраняются в S3 до ACK; review/allow публикует отдельную
рабочую копию с атомарным workspace checkpoint. Локальные проверки входят в
этот этап. Реальный Cloud S3/Pod replacement, MCP/A2A и browser ingress проверяются
отдельно после передачи кода; production readiness не объявляется готовой.
Нормативный контракт — [Cloud.ru profile](../../../spec/cloudru.md).

**Цель:** разместить enterprise-агента в Evolution AI Agents, сохранив наш UI,
настройки, MCP, A2A, вопросы владельцу и восстановление долгих задач.

**Целевая архитектура после первого trial:** приложение в обычном Pod, платформенная PostgreSQL
для состояния, S3 для содержимого файлов, ограниченный RAM-каталог для рабочих копий.
Владельцы входят по отдельному паролю. Python и команды исполняются через текущий
sandbox; его работоспособность в целевом Pod остаётся обязательным release gate.
Локальная реализация первого trial разрешена до доступа к этому Pod.

**Основа:** текущие Python runtime, PostgreSQL stores и React UI. Репозиторий
`core-agent-ai-agents` используется как работающий пример размещения и источник
отдельных решений, а не как база для слияния проектов.

## 1. Зафиксированные ограничения и решения

- По информации пользователя, Pod не имеет постоянного диска; доступная ему
  записываемая файловая система временная и расходует оперативную память.
- Пользователь уточнил, что платформа передаёт S3 credentials. Поэтому отсутствие
  смонтированного S3 не означает отсутствие постоянного хранилища по API.
- Специальные настройки безопасности Pod, используемые нашим sandbox,
  недоступны для самостоятельного задания. Фактические разрешения ещё не проверены.
- Keycloak в целевой схеме отсутствует. Внешние вызовы проходят платформенный
  прокси. Пользователь выбрал дополнительный пароль владельцев через ENV.
- Для проектирования считаем, что прокси не передаёт приложению проверенную
  identity внешнего агента. Это принятое ограничение, а не результат live-проверки.
- Платформенные API-ключи выпускаются вручную в личном кабинете Cloud.ru:
  отдельный статический ключ сервисного аккаунта для каждого внешнего агента;
  отзыв — там же. Дополнительного app peer secret нет. Внешние peers имеют
  общий scope: известная external Task доступна любому из них; UUID не является
  authentication. Внешние list/enumeration полностью запрещены.
- Полный будущий S3 snapshot включает весь проект, скрытые файлы, изменения
  и удаления. Пустой чат не создаёт archive/blob; неизменное содержимое повторно
  не сохраняется. Первый trial временно использует RAM без этих гарантий.
- Для терминала, Python и фоновых команд выбран существующий sandbox.
  Автоматического перехода на обычный subprocess, remote execution или профиль
  без execution при недоступности sandbox не предусматривается.
- MCP-подключения остаются в нашем UI и сохранённых настройках. Перенос
  платформенного механизма MCP через ENV не требуется. В новом профиле
  автоматически подставленные `MCP_URL` и связанные credentials не должны
  незаметно импортироваться или заменять сохранённые подключения.
- По уточнению пользователя, PostgreSQL предоставляет сама платформа через ENV;
  работающий `core-agent-ai-agents` уже поддерживает эти параметры подключения.
  Отдельная внешняя PostgreSQL не требуется. До переноса проверяются подключение
  enterprise-профиля, совместимость схемы и полномочия для миграций.

Официальные материалы Cloud.ru подтверждают запуск собственного Docker-образа,
платформенные параметры модели и публичного URL. Они не подтверждают передачу
проверенной личности вызывающего внутрь контейнера или нужные sandbox permissions.
См. [пример запуска](https://github.com/cloud-ru/evo-ai-agents-labs/tree/main/lab2-adk-agent)
и [переменные платформы](https://cloud.ru/docs/ai-agents/ug/topics/concepts__variables-agents).

## 2. Что мешает запустить текущий образ

| Область | Что сейчас происходит | Следствие для переноса |
|---|---|---|
| Авторизация | `AuthSettings` в production требует Keycloak; owner API и настройки подключаются вместе с ним | Нужен альтернативный способ создать доверенный `Principal`, сохранив разграничение прав |
| Браузер | UI использует Keycloak/PKCE; API требует Bearer и не отправляет cookies | Нужен режим входа по паролю и cookie-сессия |
| Выполнение | `create_app()` безусловно запускает sandbox preflight; `without_terminal` оставляет Python | Одной смены ENV недостаточно даже для режима без команд |
| Файлы | `PostgresArtifactStore` хранит в БД метаданные, а bytes — в FS; uploads/quarantine тоже файловые | После замены Pod останутся записи о потерянных файлах |
| Рабочие каталоги | Постоянные chat sessions создаются без snapshot store | Существующий recovery не восстановит произвольный проект из потерянной RAM |
| Фоновая работа | Recovery, cron, A2A polling и push выполняются без активного HTTP-запроса | Нужны CPU вне запросов и запрет scale-to-zero либо отдельный постоянно работающий worker |

Точки входа: [auth.py](../../../core_agent/auth.py),
[app.py](../../../core_agent/app.py), [auth.ts](../../../ui/src/auth.ts),
[api.ts](../../../ui/src/api.ts), [artifacts.py](../../../core_agent/artifacts.py),
[chat_files.py](../../../core_agent/chat_files.py),
[execution.py](../../../core_agent/execution.py).

В reference-репозитории входная identity по умолчанию общая, а команды запускаются
обычным `subprocess` без OS security boundary. Его успешный запуск не доказывает
изоляцию внешних callers или безопасность исполнения внутри enterprise Pod.

## 3. Авторизация: платформенный прокси и пароль владельцев

Для владельцев добавляем один общий пароль из deployment secret, переданного
через ENV. Backend проверяет пароль и выдаёт случайную непрозрачную сессию.
В PostgreSQL хранятся digest сессии и срок действия; браузер получает
`Secure`, `HttpOnly`, host-only cookie. Пароль не сохраняется в браузерном
хранилище, настройках модели, истории, telemetry или исходящих запросах.

Необходимое поведение:

- Сессия на 8 часов, cookie `__Host-core-agent-owner` с `SameSite=Lax`,
  logout и истечением срока; смена пароля отзывает старые сессии
  на всех экземплярах. Проверка пароля и лимит попыток выполняются на сервере.
- Проверка точного Origin и CSRF-защита изменяющих запросов, включая login.
  Одного SameSite недостаточно при соседних поддоменах платформы.
- Сессия создаёт существующий owner `Principal`. Она открывает настройки,
  историю и приватные вопросы владельцам, но не меняет исходного owner задачи.
- Внешний A2A-вход никогда не получает owner authority из owner cookie.
  Cookies, пароль и входные platform credentials не пересылаются в MCP/A2A.
- Общий пароль означает общий круг владельцев без персонального аудита каждого
  человека. Для этого проекта такой режим проще индивидуальных учётных записей.
- Keycloak-профиль остаётся доступным для прежних развёртываний. Меняется
  механизм аутентификации, а не правила доступа к Task/HITL/settings.

**Проверка на первом целевом запуске:** браузер должен суметь открыть `/ui/`, assets
и login endpoint через внешний прокси. Если прокси требует API-key в
`Authorization` для каждого запроса и не поддерживает браузерную сессию,
обычная навигация не пройдёт. Тогда нужен поддержанный платформой browser access
или отдельная политика доступа для UI. Встраивать общий platform key в JS/URL
не предлагается.

**Вкладка «Доступ к агенту» в Cloud.ru-профиле:** вместо управления Keycloak
показывает адрес A2A и инструкцию:

1. В личном кабинете Cloud.ru открыть сервисный аккаунт, используемый для
   аутентификации, и создать API-ключ для сервиса Evolution AI Agents.
2. Передать одному внешнему агенту один отдельный ключ и адрес A2A. По названию
   ключа в личном кабинете должно быть понятно, какому агенту он выдан.
3. Чтобы отозвать доступ этого агента, выключить или удалить его ключ в Cloud.ru.

Речь о статическом API-ключе, а не о передаче пары Key ID/Key Secret для получения
IAM-токенов. Выдачу, хранение и отзыв платформенных ключей наш UI не автоматизирует.
Отзыв доступа не означает отмену уже принятой задачи: её lifecycle остаётся
отдельным. Основание: [аутентификация AI Agents](https://cloud.ru/docs/ai-agents/ug/topics/api-ref__authentication)
и [управление API-ключами](https://cloud.ru/docs/console_api/ug/topics/guides__static-api-keys?source-platform=Evolution).

**Согласованный external scope:** сервер создаёт один стабильный external actor
на deployment/company. Разные Cloud keys дают отдельный отзыв на proxy, но не
отдельных владельцев в приложении. Известные external Task/context IDs доступны
допущенным peers для Get/Subscribe/follow-up/Cancel; owner Tasks/private API/HITL
закрыты. External listing запрещён на обеих bindings, без раскрытия IDs/count.
Дедупликация messageId общая. Identity из тела/UUID/заголовка не принимается.
Код по-прежнему изолирован в workspace текущего чата. Это принятое сокращение
per-peer confidentiality; отдельный app secret больше не рассматривается.

## 4. Хранилище: PostgreSQL + S3 + RAM

| Где | Что сохраняем |
|---|---|
| Платформенная PostgreSQL | Чаты, Tasks, checkpoints, inbox/dedup, leases, настройки, encrypted credentials, HITL/guardrails, память, remote bindings, очереди и manifests файлов |
| S3, следующий этап | Полный проект чата, включая скрытые файлы, каталоги/permissions, изменения/удаления; attachments, quarantine, typed results и offload bytes |
| RAM FS | На первом trial — временные workspace и file bytes без сохранности после замены Pod; после S3 этапа — ограниченный cache проверенных полных snapshots |
| Образ | Код, зависимости, закреплённые skills/resources; установка пакетов при старте не требуется |

Используем PostgreSQL, предоставляемую Evolution AI Agents. В reference
`core-agent-ai-agents` параметры объявлены в `.env.example` и используются в
`core_agent/app.py`: `_session_database_url()` собирает DSN, а `_state()` передаёт
его в `PostgresDatabase` для общих stores сессий, Tasks, workflow и checkpoints.
Тот же путь уже есть в enterprise [app.py](../../../core_agent/app.py) и
[шаблоне ENV](../../../.env.example); новый backend для этого не нужен.

- Для состояния явно задаются `SESSION_STORAGE_TYPE=postgres` и
  `TASK_STORAGE_TYPE=postgres`. Параметры подключения: `SESSION_POSTGRES_PROTOCOL`,
  `SESSION_POSTGRES_HOST`, `SESSION_POSTGRES_PORT`, `SESSION_POSTGRES_DATABASE`,
  `SESSION_POSTGRES_USER`, `SESSION_POSTGRES_PASSWORD`. Приоритет источников DSN:
  `SESSION_DATABASE_URL`, затем `SESSION_POSTGRES_*` при непустом host,
  затем `TASK_POSTGRES_URL`, затем `DATABASE_URL`. Tasks используют общий пул.
- Для включённой памяти задаётся `MEMORY_STORAGE_TYPE=postgres`. При отсутствии
  `MEMORY_POSTGRES_HOST` память использует общий пул; непустой host выбирает
  отдельный DSN из `MEMORY_POSTGRES_PROTOCOL`, `MEMORY_POSTGRES_HOST`,
  `MEMORY_POSTGRES_PORT`, `MEMORY_POSTGRES_DATABASE`, `MEMORY_POSTGRES_USER` и
  `MEMORY_POSTGRES_PASSWORD`. Для отдельной базы также нужна подготовленная схема.
- В production сохраняется `DATABASE_AUTO_MIGRATE=false`. Отдельный шаг
  `core-agent-db migrate` в [database.py](../../../core_agent/database.py) читает
  `DATABASE_MIGRATION_URL` либо `DATABASE_URL`, но не собирает URL из
  `SESSION_POSTGRES_*`. Для него необходимо явно передать DSN платформенной базы
  с DDL credential; `DATABASE_APP_ROLE` задаёт роль приложения для выдачи DML grants.
  Serving process использует runtime credential и проверяет версию схемы.

Подтверждена поддержка ENV в коде, а не готовность платформенной базы к
enterprise-схеме. На этапе проверки платформы проверяются соединение из целевого Pod и требования
TLS, версия PostgreSQL, доступность `pgvector` для индекса векторного поиска, права
на миграции и выдачу grants, лимиты соединений, совместимость существующих данных
и порядок backup/restore. Отсутствие `pgvector` не блокирует подключение: текущая
память поддерживает расчёт косинусной близости в процессе по сохранённым векторам.
Эти проверки определяют настройку платформенной базы;
развёртывание отдельной PostgreSQL не является предпосылкой плана.

S3 используем напрямую по API. Это не требует FUSE и специальных mount permissions.
Монтирование рассматривается только как отдельная проверяемая возможность
платформы, а не основа плана: [rclone в контейнере](https://rclone.org/install/)
требует дополнительных разрешений, а [s3fs](https://github.com/s3fs-fuse/s3fs-fuse#limitations)
не предоставляет атомарное переименование и координацию нескольких клиентов.
Текущий файловый протокол использует `rename`, `fsync` и защищённые POSIX paths;
перенести его целиком на S3 mount без изменения гарантий нельзя.

Порядок сохранения:

1. Создать durable staging intent с ограниченным сроком и неизменяемым object ID.
2. Загрузить bytes в S3, проверить подтверждение, размер и digest. При неизвестном
   результате PUT сверить тот же объект; не создавать новую логическую операцию.
3. Повторно проверить lease, scope, generation, отмену и revision, затем закрепить
   manifest/reference вместе с receipt/checkpoint в PostgreSQL.
4. Только после этого подтвердить приём или публикацию. Quarantine не становится
   доступным модели до разрешения; выдача файлов остаётся авторизованной.
5. Материализовать нужные разрешённые файлы в RAM с проверкой безопасного пути,
   размера и digest. Не восстанавливать весь bucket при запуске.

S3 I/O выполняется без удержания DB locks/соединения. БД и S3 не имеют общей
транзакции: незакреплённые объекты удаляет ограниченный sweeper, учитывающий
staging leases, ссылки и сроки backup retention. Object key или digest не дают
права читать чужой файл. Presigned/public URLs не заменяют текущие проверки доступа.

Нужны лимиты на общий RAM, workspace, uploads, параллельные скачивания и
распакованный размер. Стабильный encryption key сохраняется отдельно от БД/S3.

**Полный рабочий каталог — обязательная часть будущего S3 этапа.** Сохранение вложений и опубликованных
артефактов ещё не сохраняет промежуточный проект. Для продолжения команд после
замены Pod нужны версии manifest, удаления/tombstones и фиксация файлов на
безопасных границах действий, согласованная с workflow checkpoint. Синхронизация
только при старте/завершении задачи или периодический `sync` этого не гарантируют.
Неизвестный результат команды нельзя повторять ради восстановления файлов.
Пустой чат/дерево не создаёт пустого archive/blob; повторный неизменный checkpoint
переиспользует manifest и bytes. Каталог создаётся лениво; служебные manifests
attachments/A2A не означают, что image skills нужно копировать в проект.

Reference содержит S3 backend, но переносить весь `ArtifactService` не нужно.
Сначала переиспользуем пригодные тестовые случаи и проверим transport отдельно:
точный endpoint, формат ключей, session token/ротацию, запрет неожиданных redirects,
bounded чтение и права Put/Get/Delete. Самодельную реализацию подписи не копируем
автоматически; используем поддерживаемый S3 SDK, если подходящего клиента нет.

## 5. Терминал, Python и работа с файлами

Выбран текущий sandbox: Bubblewrap, изоляция файлов и процессов, сетевые
ограничения и seccomp. Через него продолжают работать `core_terminal_exec`,
`core_python_exec`, фоновые команды `core_task_start` и nested вызовы.
S3/DB/owner credentials приложения не передаются выполняемому модельному коду.

Обязательный deployment gate — реальный preflight и terminal/Python/background smoke в целевом
Pod. Проверяются вложенные namespaces, mount/proc, seccomp, `/dev/net/tun`,
сетевые helpers и доступные конечные CPU/RAM/PID limits. Существующий проверенный
[Pod manifest](../../../deploy/kubernetes/sandbox-pod.yaml) использует
`hostUsers: false`, `procMount: Unmasked` и отдельный OCI seccomp profile;
при ограничениях AppArmor нужна [подготовка узла](../../../deploy/kubernetes/apparmor/README.md).
Наличие этих возможностей в Evolution AI Agents пока не доказано.

Если preflight не проходит из-за запретов платформы, выбранный профиль нельзя
считать готовым к развёртыванию. Потребуется разрешение нужных настроек со стороны
Cloud.ru либо новое согласованное архитектурное решение. Проверки не отключаются,
обычный subprocess или другой execution backend не включается как fallback.

`TerminalSessionManager` и `ChatWorkspaces` сохраняются: они нужны и выполнению
команд, и работе с файлами. Для S3 добавляется восстановление разрешённых рабочих
копий в RAM и фиксация workspace на согласованных границах действий; сам S3 не
предоставляет требуемую процессам POSIX-файловую систему.

## 6. Первый trial и последующие этапы

Текущий запрос — минимальный запускаемый образ с debug logs. Первый trial задаёт
`CORE_AGENT_ENVIRONMENT=development`, `CORE_AGENT_AUTH_MODE=cloudru`, обязательные
platform PostgreSQL, tenant, owner password и HTTPS `AGENT_URL` (либо `URL_AGENT`).
Дополнительный trial switch не нужен. Production Cloud startup пока отклоняется
до реализации полного storage; временные файлы не объявляются durable.

В первый slice входят owner password/DB sessions, существующий UI, общий external
scope без listing, сохранённые MCP/settings и безопасные startup diagnostics.
`CORE_AGENT_STARTUP_DIAGNOSTICS=true` показывает конечные stage/status/reason и
разрешённые факты наличия ENV/состояния sandbox. Нет dump ENV, secrets, DSN,
raw URL/body/private paths или traceback. Проверки не отключаются ради старта.
Если sandbox отсутствует, logs показывают отказ, fallback не включается.

S3 SDK/backend/config, перенос existing bytes и полное workspace восстановление
не входят в первый slice. Пользователь запускает образ, передаёт logs и после
этого согласуется следующий шаг. PostgreSQL сохраняет sessions/settings/Tasks,
но workspace/upload/quarantine/result/offload bytes остаются временными.
Нельзя выдавать оставшиеся после замены Pod DB references за сохранённые файлы.

Каждый этап сохраняется проверяемым commit. Разрешение пользователя распространяется
на связанные frozen spec/tests в рамках согласованного плана. Перед изменением
observable behavior обновляются нормативные документы, acceptance и hash lock.

| Этап | Работы | Критерий завершения |
|---|---|---|
| 1. Минимальный trial | Cloud config/auth, UI login/logout/manual keys, persisted-only MCP, platform PostgreSQL, текущий sandbox, sanitized startup diagnostics, запуск образа | Локальные checks проходят; пользователь получает образ/инструкцию и может передать безопасные startup logs |
| 2. Проверить реальную платформу | Proxy/browser/cookies/SSE, sandbox terminal/Python/background, DB migration/grants, writable paths, CPU/idle/replicas/SIGTERM | Реальные результаты из целевого Pod; неизвестные permissions не заменены предположениями |
| 3. Полное S3-хранилище | Bytes всех storage domains, полное дерево workspace, безопасные snapshots/restore/cleanup, empty/unchanged semantics, bounded SDK I/O | Пустая FS нового Pod восстанавливает все committed bytes; fault/race/recovery checks проходят без replay |
| 4. Подтвердить долгую работу | A2A waits, cron/push, rolling overlap, execution ownership и reconciliation | Подтверждённые side effects не повторяются, cleanup barrier сохраняется |
| 5. Production перенос | Migrations, backup/import, реальные Cloud gates, проверенный rollback и обновлённый release status | Полный профиль проходит CLD-AC; существующие файлы/owner IDs не теряются и не переназначаются автоматически |

В начале — один экземпляр, без scale-to-zero, с CPU вне HTTP-запросов. Это не
заменяет fencing: rolling update может временно оставить два живых процесса.
Масштабирование на несколько экземпляров включаем после проверки leases,
owner sessions, файловой материализации и execution ownership на этом профиле.

Для модели проверяем реальные model IDs, `/models`, tool calls, strict structured
output, reasoning и compaction. Префикс `hosted_vllm/` из платформенного примера
нельзя вслепую считать wire ID для нашего прямого OpenAI-compatible adapter.
Phoenix подключаем через текущий OTEL exporter при необходимости; он не становится
обязательной зависимостью запуска. Встроенный UI требует проверки не только A2A,
но и static assets, API, cookies, SSE, body limits и proxy timeouts.

## 7. Проверки и переход

Минимальные новые сценарии:

- Вход/выход владельца, неверный пароль и перебор, CSRF, смена пароля, рестарт,
  два экземпляра, запрет owner API внешнему caller и отсутствие secrets в logs.
- Отдельные Cloud.ru-ключи для двух callers: отзыв одного закрывает новые обращения
  с ним, второй продолжает работать. Оба имеют общий external scope и dedup;
  известная external Task доступна обоим, listing полностью закрыт. Owner-private
  данные остаются закрытыми.
  Owner-просмотр external Task не меняет адресата ответа.
- Реальные terminal/Python/background вызовы через текущий sandbox в целевом Pod;
  недоступная изоляция блокирует запуск. Обход через прямой dispatch/nested вызовы
  и fallback на обычный subprocess отсутствуют.
- Удаление Pod с пустой новой FS во время A2A/HITL и после принятия файла;
  восстановление контекста, настроек, решений, artifacts и разрешённых копий.
- Сбой до/после S3 upload и DB commit, неизвестный PUT, недоступность DB/S3,
  переполнение RAM, stale worker, cleanup и запрет повторения неизвестного Send.
- Работа фоновых задач в idle; SIGTERM/rolling replacement; файлы и ответы
  доступны через реальный Cloud proxy и браузер.

Опираемся на существующие `tests/test_auth.py`, `test_interactions.py`,
`test_postgres_persistence.py`, `test_chat_files.py`, `test_remote_operations.py`,
`test_remote_runtime.py`, `test_workspace_cleanup_api.py` и browser suite.
Новые проверки профиля входят в обычный CI. Существующие sandbox tests остаются
обязательными; для Cloud.ru дополнительно нужен реальный smoke в целевом Pod,
который нельзя заменить mock-проверкой или успешным запуском в другом кластере.

Стандартные команды после реализации:

```sh
uv run ruff check core_agent tests
uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v
# TEST_DATABASE_URL и TEST_POSTGRES_CONTAINER заранее указывают на тестовую БД.
uv run python -m unittest discover -s tests -v
cd ui
npm run typecheck
npm run build
```

Для существующих данных: остановить приём новых задач, согласованно сохранить
БД/файлы/ключи, перенести bytes в S3 с проверкой digest, применить миграции отдельной
командой, проверить восстановление с пустой FS, затем переключить endpoint.
Не переносить owner IDs между auth-провайдерами по догадке: существующие внешние
чаты требуют явного identity mapping либо остаются архивом старого deployment.
После новых записей нельзя просто восстановить старый backup или старый образ
без проверенной совместимости схемы и storage references.

## 8. Что проверяем после logs первого trial

Повторного выбора owner password, общего external scope без app secret,
ручных platform keys, платформенной PostgreSQL и текущего sandbox не требуется.
Полный S3 workspace согласован как будущий этап; его реализации нет в первом trial.

- Доступность текущего sandbox в целевом Pod: namespaces, seccomp, TUN и limits.
- Browser access через proxy: `/ui/`, assets, login, cookies, API и SSE без ключей в JS.
- Platform PostgreSQL: TLS, schema/grants, миграционные права и connection limits.
- Фоновый CPU без HTTP, SIGTERM и rolling overlap. Один постоянно работающий
  экземпляр без scale-to-zero не снимает требований fencing.
- Для следующего S3 этапа: endpoint/bucket/prefix, формат/ротация credentials,
  session token, Put/Get/Delete и finite limits. Непроверенный mount не используется.

Local unit/browser checks и диагностический запуск не доказывают production
storage durability. Следующий объём определяется по фактическим logs и не
объявляется автоматически завершённым после первого успешного ответа модели.
