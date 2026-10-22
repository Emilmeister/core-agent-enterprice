# Первый запуск в Cloud.ru Evolution AI Agents

Этот запуск нужен для проверки образа и получения диагностических logs из
целевого Pod. Используйте `development` + `cloudru`: production Cloud.ru пока
запрещён. PostgreSQL сохраняет состояние и owner-сессии; workspace, uploads,
quarantine и результаты в RAM могут исчезнуть при замене Pod. Полное S3-хранилище
остаётся следующим этапом. Контракт: [spec/cloudru.md](../spec/cloudru.md).

Для первого запуска включите диагностические logs без содержимого запросов:

```dotenv
CORE_AGENT_STARTUP_DIAGNOSTICS=true
LOG_LEVEL=INFO
CORE_AGENT_LOG_CONTENT=false
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
```

## Образ и окружение

Для целевого Pod amd64 соберите текущий [Dockerfile](../Dockerfile) из корня
репозитория:

```bash
docker build --platform linux/amd64 -t core-agent:cloudru-trial-20261021 .
```

Загрузите образ в доступный платформе registry. Для другого target architecture
выберите соответствующий native Linux platform. UI и sandbox helpers входят
в образ; команда запуска — `core-agent`. Listener должен быть доступен только
через платформенный proxy, проверяющий входящие Cloud API keys.

Передайте параметры через настройки окружения/секретов платформы. Сам процесс
`core-agent` не читает `.env` автоматически. Минимальная основа:

```dotenv
CORE_AGENT_ENVIRONMENT=development
CORE_AGENT_AUTH_MODE=cloudru
CORE_AGENT_TENANT_ID=cloudru-trial
AGENT_URL=https://your-agent.example
HOST=0.0.0.0
PORT=8000
SESSION_STORAGE_TYPE=postgres
TASK_STORAGE_TYPE=postgres
CORE_AGENT_MEMORY=disabled
DATABASE_AUTO_MIGRATE=true
CHAT_WORKSPACE_ROOT=/tmp/core-agent/chats
DURABLE_STORAGE_ROOT=/tmp/core-agent/durable
LOCAL_WORKSPACE_ROOT=/tmp/core-agent/runs
```

Замените `AGENT_URL` каноническим HTTPS origin без дополнительного пути; platform
`URL_AGENT` используется, если `AGENT_URL` отсутствует. Выберите отдельный tenant
для trial. Три filesystem root должны быть записываемыми и не пересекаться.
Используйте platform `PORT`, если она задаёт его самостоятельно.

Дополнительно обязательны:

- `CORE_AGENT_OWNER_PASSWORD`: отдельный secret длиной 16–1024 символа. При
  ротации обновите все экземпляры; ранее активированный и затем заменённый
  пароль больше нельзя использовать. Старый replica с ним не запустится.
- Platform PostgreSQL: `SESSION_POSTGRES_HOST`, `SESSION_POSTGRES_PORT`,
  `SESSION_POSTGRES_DATABASE`, `SESSION_POSTGRES_USER`, `SESSION_POSTGRES_PASSWORD`
  и при необходимости `SESSION_POSTGRES_PROTOCOL`. Полный `SESSION_DATABASE_URL`
  имеет приоритет; далее используются эти части, `TASK_POSTGRES_URL`, `DATABASE_URL`.
- `LLM_API_FORMAT`, `LLM_API_BASE`, `LLM_MODEL`, `LLM_API_KEY` для доступной модели.
  Остальные поддержанные model settings — в [ENV-примере](../.env.example).
- Стабильный `PUSH_NOTIFICATION_ENCRYPTION_KEY` в формате Fernet для encrypted
  settings/credentials. Сохраняйте его отдельно от БД и между перезапусками.
- `SANDBOX_DNS_SERVERS` и `SANDBOX_DENIED_CIDRS` с реальными защищёнными сетями
  deployment. Default `SANDBOX_BACKEND=bubblewrap` требует Linux namespaces,
  seccomp, TUN и resource limits;
  параметры и outer security profile описаны в [инструкции](../deploy/kubernetes/apparmor/README.md)
  и [Pod fixture](../deploy/kubernetes/sandbox-pod.yaml).

MCP настраиваются в owner UI. Автоматические `MCP_URL`/`MCP_HEADERS_JSON` здесь
игнорируются. Platform `ARTIFACT_STORAGE_*`, `ARTIFACT_S3_*`, `ARTIFACT_MONGODB_*`
и `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS` тоже игнорируются: они не включают S3,
не импортируют прежние blobs и не мешают диагностическому запуску. Default
Keycloak profile продолжает отклонять эти retired artifact settings.

## Ограниченный Landlock trial

Для Cloud development можно явно выбрать альтернативный backend:

```dotenv
CORE_AGENT_ENVIRONMENT=development
CORE_AGENT_AUTH_MODE=cloudru
SANDBOX_BACKEND=landlock
```

Контракт и открытые проверки — [CLD-05-L](../spec/cloudru.md#cld-05-l-явный-development-backend-landlock)
и [CLD-LANDLOCK](../spec/implementation-status.md). Это выбор оператора;
отказ Bubblewrap сам его не включает. Production, Keycloak и test environment
с этим выбором отклоняются. Без переменной остаётся Bubblewrap.

Запускайте `core-agent` непосредственно как PID 1 выделенного container PID
namespace, без init/supervisor wrapper и shared process namespace. Процесс
должен работать под non-root UID и иметь нулевые
effective/permitted/inheritable/ambient capabilities.
Если платформа не обеспечивает эти условия, данный trial не запускается.
Preflight обязан реально применить Landlock ABI >= 4 и seccomp, создать
notification listener и передать socket FD ограниченному child через ADDFD.
Одних успешных диагностических probes недостаточно; отказ закрывает serving.
Запись `startup.sandbox_backend` показывает выбранный backend перед preflight.
Проверка реальной FD injection использует отказ запрещённого private HTTP
destination и не требует доступности внешнего сайта.

Ограничения первого backend:

- Команды работают с настоящим scoped workspace: `HOME` и начальный `cwd`
  указывают на него. Используйте относительные пути или `$HOME`; `/workspace/...`
  принимается только параметром `cwd` tools и не существует как shell alias.
- Временные файлы создавайте через `$TMPDIR`/`tempfile`; этот частный каталог
  находится вне workspace, очищается после execution и не сохраняется в проекте.
  Общий `/tmp` недоступен.
- Сеть доступна HTTP/HTTPS клиентам, поддерживающим переданный proxy.
  Прямые sockets/IP, UDP, произвольные TCP протоколы и чужие Unix sockets
  недоступны. DNS и destination checks используют обязательные
  `SANDBOX_DNS_SERVERS`/`SANDBOX_DENIED_CIDRS`; удаление proxy ENV не открывает сеть.
- Новые защищённые owner uploads отклоняются с `CAPABILITY_DISABLED` до
  admission; прежние quarantined protected batches не публикуются в workspace.
  Если в `attachments` уже есть файлы, terminal/Python/background execution
  возвращает `EXECUTION_ENVIRONMENT_UNAVAILABLE` до запуска. Сохранённые bytes,
  history и owner file access остаются. Для execution используйте чат без
  защищённых вложений; созданные агентом файлы и writable A2A файлы `a2a/`
  доступны по прежнему контракту.
- Git init/clone/checkout используют доверенную обработку chmod/timestamps
  внутри workspace/TMPDIR. Metadata paths проверяются через закреплённые FD;
  чужие пути, special mode bits, chown, xattr, inotify и сигналы чужим
  процессам запрещены.
- Timeout/cancel и завершение команды подтверждают остановку всех её потомков.
  Неопределённый результат сохраняет прежний cleanup/reconciliation barrier.

После успешного preflight в целевом Pod отдельно проверьте terminal, Python
с `tools.call`, background, отмену/timeout, разрешённый HTTP/HTTPS и отказ
запрещённых destinations. Сохраните безопасные результаты этих проверок вместе
со startup logs. До этого target gate остаётся открытым; S3 durability этот
backend не добавляет.

Для локальной ARM64 проверки при отсутствии Landlock в Docker host kernel
есть отдельный [VM fixture](../deploy/landlock-native-vm.sh):

```bash
LANDLOCK_TEST_IMAGE=core-agent-enterprice-agent:latest bash deploy/landlock-native-vm.sh
```

Он устанавливает тестовые dependencies только в одноразовый Docker container,
загружает packaged Debian kernel в QEMU TCG, использует read-only копию образа
и исходников и отдельные guest tmpfs/cgroups. Python tests исполняются как
непривилегированный PID 1 guest; явный outer test filter запрещает
mount/unshare/setns. Это реальная kernel-проверка Landlock/seccomp, но не
воспроизведение security profile Cloud.ru. Скрипт не меняет Docker Desktop или
действующие контейнеры и считает успехом только явный результат suite.
`LANDLOCK_VM_LOG` задаёт путь полного guest log; без него log остаётся во
временной папке. Production image и target Cloud gate этим не заменяются.

## Миграция и первый вход

Для первого trial используйте **новую выделенную БД** и credential с DDL правами:
`DATABASE_AUTO_MIGRATE=true` в development применит схему при старте `core-agent`,
без отдельного exec/job. Память в минимальной конфигурации отключена.

Для существующей или общей БД auto-migration не используйте: operator заранее
выполняет `core-agent-db migrate` с полным `DATABASE_MIGRATION_URL` либо
`DATABASE_URL` и, для отдельной runtime-роли, `DATABASE_APP_ROLE`. Migration CLI
не собирает `SESSION_POSTGRES_*`. Затем запускайте с `DATABASE_AUTO_MIGRATE=false`.

Откройте `<AGENT_URL>/ui/` и введите owner password. Proxy должен поддерживать
browser access к UI, assets и API; platform key нельзя встраивать в JS/URL.
Сессия — защищённая HttpOnly cookie на 8 часов, выход отзывает её в PostgreSQL.
Для внешнего peer используйте `<AGENT_URL>/` и отдельный статический
Cloud API key; выдача/отзыв выполняются вручную в Cloud console. Peers имеют общий
external scope для известных Task IDs; перечисление Tasks закрыто, owner данные
им недоступны.

JSON-RPC принимает `POST /`; HTTP+JSON использует `/message:send`,
`/message:stream` и `/tasks/...` от корня. Agent Card доступна по
`/.well-known/agent-card.json` и `/.well-known/agent.json`; адрес в ней берётся
из настроенного `AGENT_URL`. Прежний `/a2a/external/` работает как alias без
redirect с тем же scope и дедупликацией. Owner A2A остаётся на `/a2a/owner/`;
owner cookie не расширяет права внешних вызовов через корень или alias.

`GET /` и `HEAD /` используют ту же проверку готовности, что `/health/ready`:
200 при исправном sandbox и доступной БД с совместимой schema, иначе 503.
HEAD возвращает статус без body. `/health/live` остаётся отдельным liveness
endpoint. Эти root routes включены только в Cloud.ru profile.

## Какие logs передать

Сохраните stdout/stderr от начала запуска до готовности либо первого отказа.
`startup.platform` показывает ограниченные факты о helpers, namespaces, cgroups
и TUN; `startup.stage` — стадии configuration, model_configuration, database,
sandbox, runtime, routes, recovery и общий startup. `startup.storage` сообщает
`backend=temporary-posix`, `durable=false`. `application_ready` означает сборку
ASGI application; фактическое открытие listener подтверждают сообщения Uvicorn
и успешные `/health/live`, `/health/ready` через платформу.

Если после успешного старта Pod перезапускается, сохраните platform event о
завершении и результаты запросов к `/`, root Agent Card и `/health/ready`.
Корневые маршруты обеспечивают discovery и readiness, но причина перезапуска
без этих данных остаётся неподтверждённой.

Перед штатной проверкой sandbox первые записи `startup.sandbox_probe` —
`landlock_abi`, `landlock_filesystem` и `seccomp_filter`. Отдельные доверенные
процессы Python с `-I -S` без наследования ENV проверяют номер Landlock ABI,
а затем фактическое действие ограничений: Landlock сохраняет чтение `/dev/null`
и запрещает открытие `/dev/zero` с `EACCES`; seccomp меняет успешный прямой
syscall `getppid` на `EPERM` после установки явного правила. Доступ к обоим
устройствам проверяется до ограничения. Эти probes не читают пользовательские
файлы, не создают файлы, не используют сеть и не ограничивают основной процесс.

Следующие записи отдельно показывают создание user namespace,
отображение UID/GID и доступность mount/PID/IPC/network/UTS
namespaces. Затем выполняются три фиксированных запуска Bubblewrap с `/usr/bin/true`:
минимальный, с `/proc` и со всеми namespaces, `/proc`, `/dev`, временным `/tmp`.
Они не используют TUN, код модели или ENV приложения. Это позволяет отделить
отказ создания namespace от отказа монтирования `/proc`; отсутствие TUN относится
к последующей проверке полного сетевого профиля.

Выполнение одной проверки занимает не более 2 секунд, общий бюджет запуска
проверок — 10 секунд; на остановку её процессов отдельно отводится до 1 секунды.
В logs остаются конечные status/reason/cleanup, а operation/errno/returncode и
номер ABI добавляются, когда доступны. Stderr ограничен 4096 байтами и целиком
не публикуется. Отказы видны и при
`LOG_LEVEL=CRITICAL`. Если остановка диагностического процесса не подтверждена,
startup прекращается. `EPERM` сам по себе не определяет виновника: AppArmor,
seccomp и другие ограничения могут вернуть одинаковую ошибку.

Диагностика выводит конечные status/reason-коды и наличие конфигурации без её
значений. Не прикладывайте ENV dump, пароли, DSN, tokens или содержимое запросов.
Передайте записи `startup.sandbox_probe` для `landlock_abi`,
`landlock_filesystem`, `seccomp_filter`, последующие namespace/Bubblewrap probes
и stage/reason обязательного preflight выбранного backend. Optional probes
могут показывать недоступность Bubblewrap даже при явно выбранном Landlock;
они не выбирают backend и не меняют его ограничения. Их успех подтверждает
только проверенные действия, а готовность sandbox определяет полный preflight
выбранного backend. Локальные tests и успешная сборка образа не заменяют эту
проверку реального Pod.
