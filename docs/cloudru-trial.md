# Cloud.ru Evolution AI Agents: запуск и пакетный rollout

Cloud.ru поддерживает development trial и production configuration с обязательными
PostgreSQL/S3, стабильным encryption key и отдельными миграциями. Включённая
память требует `MEMORY_STORAGE_TYPE=postgres` в обоих режимах; `CORE_AGENT_MEMORY=disabled`
не создаёт backend. По умолчанию development файлы временные; явный S3 сохраняет
рабочие папки, опубликованные результаты и закрытые оригиналы входящих вложений
до подтверждения приёма. Production POSIX/RAM-only configuration отклоняется.
Контракт: [spec/cloudru.md](../spec/cloudru.md).

Работающий deployment можно обновить одним candidate image с прежней development
configuration. Смена `CORE_AGENT_ENVIRONMENT` — отдельный подготовленный шаг:
не меняйте этот флаг до проверки миграции, runtime role и encryption key ниже.
Допуск production startup не означает, что все real Cloud release gates пройдены.

Для первого запуска включите диагностические logs без содержимого запросов:

```dotenv
CORE_AGENT_STARTUP_DIAGNOSTICS=true
LOG_LEVEL=INFO
CORE_AGENT_LOG_CONTENT=false
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
```

## S3 и сохранность данных

Дополнительно к работающей конфигурации включите:

```dotenv
WORKSPACE_STORAGE_TYPE=s3
WORKSPACE_S3_PREFIX=core-agent
WORKSPACE_MAX_BYTES=100000000
WORKSPACE_MAX_FILES=10000
WORKSPACE_S3_MAX_INFLIGHT=4
```

Подключение берётся из `WORKSPACE_S3_ENDPOINT_URL`, `WORKSPACE_S3_BUCKET`,
`WORKSPACE_S3_REGION`, `WORKSPACE_S3_ACCESS_KEY_ID`, `WORKSPACE_S3_SECRET_ACCESS_KEY`.
Для незаданных полей используются соответствующие платформенные `ARTIFACT_S3_*`.
Платформенный endpoint вида `https://s3.cloud.ru/<bucket>` автоматически
преобразуется в адрес сервиса, только если путь точно совпадает с выбранным
бакетом (допустим один завершающий `/`). При наличии `ARTIFACT_S3_TENANT_ID`
платформенный access key передаётся в SDK как `tenant:key`; уже добавленный
совпадающий prefix не дублируется, конфликт отклоняется. Это S3 tenant,
не `CORE_AGENT_TENANT_ID`. Явный `WORKSPACE_S3_ENDPOINT_URL` должен содержать
только адрес сервиса; явный `WORKSPACE_S3_ACCESS_KEY_ID` передаётся без изменений.
Опциональный временный токен — `WORKSPACE_S3_SESSION_TOKEN`. Секреты передаются
через secret/env настройки платформы, не через UI или запрос модели.

Схема БД обновляется обычной migration-командой; development auto-migration
также поддержана. `CHAT_WORKSPACE_ROOT` и `DURABLE_STORAGE_ROOT` остаются локальными
путями. При ошибке bucket/auth запуск завершается ошибкой, временного fallback нет.
S3-режим не импортирует старые POSIX blobs и не восстанавливает уже потерянные
файлы прошлого trial. Только для нового изолированного trial выбирайте новый
tenant/prefix; при обновлении действующего S3 deployment сохраните прежние
tenant, bucket и prefix, иначе существующие ссылки не станут новой миграцией.
`LOCAL_BASE_SNAPSHOT` здесь не поддержан.

Сохраняются скрытые файлы, `.git`, permissions, изменения и удаления.
Фоновый запуск сохраняет receipt сразу, а дерево — после подтверждённой остановки.
Неизвестные команды после сбоя не выполняются повторно. Пустое дерево не создаёт
S3 blob; неизменные bytes переиспользуются. Общий восстановленный cache ограничен
двумя лимитами дерева, без автоматического удаления пользовательских файлов.

Входящий файл проходит bounded decode в памяти, затем сохраняется закрытым
оригиналом в S3. Только после записи ссылки вместе с Task/сообщением в PostgreSQL
приём подтверждается. Проверка материала может продолжаться после замены Pod.
После разрешения агент получает рабочую копию: owner-файлы в Landlock —
`inputs/<batch>/`, A2A — `a2a/`. Изменение/удаление копии сохраняется в workspace;
скачивание оригинала из проверки материала и карточки принятого файла всегда
возвращает исходные bytes, включая после изменения/удаления рабочей копии.
Карточка использует серверный scoped `download_path`; приватные S3 object keys
и подписанные URL не передаются модели или браузеру. Опубликованный результат —
отдельный immutable snapshot, дальнейшая правка рабочего файла его не меняет.
При ошибке S3 до приёма сообщение не принимается; после приёма временная
недоступность original оставляет доставку pending. POSIX/Landlock по-прежнему
не принимает protected owner uploads; старые `attachments/` не мигрируют.
Перед публикацией проверяется место для всей копии. Временное чтение оригинала
ограничено одним private cache на tenant; после аварии его lease истекает через
300 секунд, и следующий проход очистки освобождает cache. Событие
`file.cache.cleanup_pending` означает отложенную уборку временной копии.

При `LOG_LEVEL=INFO` события `workspace.storage` и `storage.gc` показывают
operation/outcome, число/размер файлов и безопасный код ошибки. В них нет
имён файлов, содержимого, ключей и подписанных URL. Startup diagnostics
показывают выбранный backend. Реальные Cloud S3/restart и API/browser original
downloads уже проверены отдельными сценариями; полный набор проверок доступа,
storage failures, background/idle и recovery остаётся условием выпуска.

## Образ и окружение

Для целевого Pod amd64 соберите текущий [Dockerfile](../Dockerfile) из корня
репозитория:

```bash
docker build --platform linux/amd64 -t core-agent:cloudru-candidate-20261024 .
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
  Он нужен и в development для подключения внешнего агента или MCP с секретом:
  без него сохранение peer credentials возвращает `REMOTE_AGENT_CREDENTIAL_UNAVAILABLE`,
  даже если owner UI и обычные задачи работают. Перед следующим rollout проверьте
  наличие этого secret именно в настройках Cloud deployment; локальный `.env`
  платформа автоматически не читает. Используйте прежний ключ, если encrypted
  state уже существует; изменение ключа не восстанавливает старые credentials.
- `SANDBOX_DNS_SERVERS` и `SANDBOX_DENIED_CIDRS` с реальными защищёнными сетями
  deployment. Default `SANDBOX_BACKEND=bubblewrap` требует Linux namespaces,
  seccomp, TUN и resource limits;
  параметры и outer security profile описаны в [инструкции](../deploy/kubernetes/apparmor/README.md)
  и [Pod fixture](../deploy/kubernetes/sandbox-pod.yaml).

MCP настраиваются в owner UI. Автоматические `MCP_URL`/`MCP_HEADERS_JSON` здесь
игнорируются. Platform `ARTIFACT_STORAGE_*`, `ARTIFACT_S3_*`, `ARTIFACT_MONGODB_*`
и `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS` сами не включают S3,
не импортируют прежние blobs и не мешают диагностическому запуску. Только
явный `WORKSPACE_STORAGE_TYPE=s3` использует fallback `ARTIFACT_S3_*`. Default
Keycloak profile продолжает отклонять эти retired artifact settings.

## Ограниченный профиль Landlock

Для Cloud development либо подготовленного production можно явно выбрать backend:

```dotenv
CORE_AGENT_AUTH_MODE=cloudru
SANDBOX_BACKEND=landlock
```

Контракт и открытые проверки — [CLD-05-L](../spec/cloudru.md#cld-05-l-явный-cloudru-backend-landlock)
и [CLD-LANDLOCK](../spec/implementation-status.md). Это выбор оператора;
отказ Bubblewrap сам его не включает. Keycloak и test environment с этим выбором
отклоняются; production требует весь состав ниже. Без переменной остаётся Bubblewrap.

Запускайте `core-agent` непосредственно как PID 1 выделенного container PID
namespace, без init/supervisor wrapper и shared process namespace. Процесс
должен работать под non-root UID и иметь нулевые
effective/permitted/inheritable/ambient capabilities.
Если платформа не обеспечивает эти условия, данный профиль не запускается.
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
  Исключение — включённый S3: private original сохраняется до приёма, после
  allow создаётся writable копия в `inputs/`. Защита прежнего `attachments/`
  и read-only вложений Bubblewrap не меняется.
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

## Подготовка production и миграция

Перед изменением работающего deployment проверьте полный набор:

| Настройка/условие | Требование production |
|---|---|
| Auth | `CORE_AGENT_AUTH_MODE=cloudru`, прежний `CORE_AGENT_TENANT_ID`, canonical HTTPS `AGENT_URL`, отдельный `CORE_AGENT_OWNER_PASSWORD`; доступ только через platform ingress |
| State | `SESSION_STORAGE_TYPE=postgres`, `TASK_STORAGE_TYPE=postgres`, доступная БД с текущей schema |
| Workspace | `WORKSPACE_STORAGE_TYPE=s3`, полная S3 configuration, прежние bucket/prefix; три записываемых и непересекающихся local roots |
| Encryption | Валидный стабильный Fernet `PUSH_NOTIFICATION_ENCRYPTION_KEY`, одинаковый у экземпляров; хранить отдельно от DB backup |
| Migration | `DATABASE_AUTO_MIGRATE=false`; отдельный DDL credential выполняет migration до serving, runtime role получает только необходимые DML grants |
| Memory | `CORE_AGENT_MEMORY=disabled` либо `optional|required` вместе с `MEMORY_STORAGE_TYPE=postgres`; без отдельного `MEMORY_POSTGRES_HOST` используется общий pool |
| Sandbox | Явный Landlock либо default Bubblewrap с настоящим успешным preflight; его невозможность блокирует запуск |
| Lifecycle | Один постоянно работающий экземпляр, CPU вне HTTP и отсутствие scale-to-zero; overlapping rollout требует подтверждения fencing |

Сначала подтвердите текущий encryption key и читаемость существующих encrypted
settings/credentials. Если прежний trial не задавал стабильный secret, нельзя
просто подставить новый ключ и обещать сохранность: подготовьте перенос encrypted
state и проверьте его на disposable копии. Не меняйте tenant, auth scope, bucket
или prefix в качестве способа исправить startup.

В операторском окружении заранее передайте полный `DATABASE_MIGRATION_URL`
либо `DATABASE_URL`; при отдельной runtime-роли задайте `DATABASE_APP_ROLE`.
Migration CLI не собирает `SESSION_POSTGRES_*`. Из checkout используются
существующие команды [README](../README.md):

```bash
uv run core-agent-db migrate
uv run core-agent-db check
```

Первую команду выполняйте с migration credential; перед `check` переключите
окружение на runtime `DATABASE_URL` без `DATABASE_MIGRATION_URL`, чтобы проверить
schema доступом приложения. В собранном образе доступны те же `core-agent-db
migrate` и `core-agent-db check`. Serving process не получает migration secret.

До rollout остановите новые admissions и writers, сохраните согласованный backup
БД и всех referenced S3 objects, а ключ шифрования — отдельно. Восстановление
проверяйте на disposable копии с пустым local cache и тем же ключом. Порядок и
граница rollback заданы в [architecture](../spec/architecture.md): до новых writes
допустим проверенный согласованный snapshot; после новых accepted records старый
backup нельзя накладывать поверх данных. Старый image должен понимать текущие
schema и storage manifests; простое переключение image не является downgrade.

Если после смены Pod execution остался неизвестным, используйте существующую
[процедуру восстановления](execution-recovery.md): внешне подтвердите остановку
точного поколения и подготовьте приватный JSON, затем выполните:

```bash
uv run core-agent-db confirm-execution-stop --file /run/secrets/stop.json
```

CLI записывает evidence, но не останавливает процессы. Потеря lease/handle или
новый пустой Pod не доказывают остановку; неизвестные команды не replay-ятся.

## Первый development trial и вход

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

JSON-RPC принимает `POST /`: прежние методы 1.0 и исторические 0.3
(`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`) через SDK adapter.
Для legacy методов header можно опустить или передать `A2A-Version: 0.3`;
методы 1.0 сохраняют прежние требования к версии. HTTP+JSON остаётся 1.0
на `/message:send`, `/message:stream` и `/tasks/...` от корня.

Карточки разделены: `/.well-known/agent-card.json` всегда имеет прежнюю
схему 1.0, а `/.well-known/agent.json` без version header или с 0.3 — схему
0.3 для встроенного чата Cloud.ru. Header `A2A-Version: 1.0` на историческом
URL возвращает canonical карточку 1.0; неподдержанная explicit версия отклоняется.
Клиенту 1.0 на старом URL нужно указать header либо перейти на canonical URL.
Адрес берётся из `AGENT_URL`, query parameters не выбирают версию.

Прежний `/a2a/external/` работает как alias без redirect с теми же правилами
версий, scope и дедупликацией: смена версии или пути не создаёт новую задачу
для того же Message. Owner A2A остаётся на `/a2a/owner/` с протоколом 1.0;
owner cookie не расширяет права внешних вызовов через корень или alias.

`GET /` и `HEAD /` используют ту же проверку готовности, что `/health/ready`:
200 при исправном sandbox и доступной БД с совместимой schema, иначе 503.
HEAD возвращает статус без body. `/health/live` остаётся отдельным liveness
endpoint. Эти root routes включены только в Cloud.ru profile.

## Один candidate и общий rollout

1. Завершите весь пакет code/spec/tests/docs и frozen hashes в постоянной рабочей
   копии. Выполните общий regression/native/browser/package прогон по
   [CI](../.github/workflows/ci.yml); новый код после проверки требует относящихся
   к нему checks, а не промежуточного Cloud deploy.
2. Соберите один candidate image, опубликуйте постоянный tag и сохраните digest
   вместе с Git revision, результатами gates и безопасными logs. Native
   `sandbox-linux` amd64/arm64 запускает `deploy/landlock-native.sh` на candidate
   с `CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1` и
   `CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1`; отсутствие enforcement — failure.
3. Подготовьте migration, backup и secrets по разделу выше. Для действующего
   development deployment сохраните текущие настройки и данные; если отдельная
   migration или стабильный encryption key ещё не подготовлены, передайте тот
   же candidate для проверки в development с явно записанными production blockers.
4. Обновите deployment один раз этим digest. Сохраните `/health/live`,
   `/health/ready`, root Agent Card, browser login и terminal/Python preflight
   evidence; проверяйте оставшуюся матрицу на этом же образе в отдельных чатах.
5. Для release нужны реальные idle/background/cron без открытого браузера,
   owner/HITL/time/remote waits, SIGTERM во время активной команды, cold restore,
   rolling overlap и отсутствие второго writer/повторных side effects. Добавьте
   isolation/network/security, отзыв platform key, runtime grants, storage failures,
   backup/restore и telemetry; результаты записывайте отдельно для каждого gate.
6. При непрошедшем gate сохраните ограниченный статус candidate и конкретный
   blocker. Выпуск подтверждается только evidence; статус config support не
   превращает development smoke в production approval.

Cloud.ru сохраняет согласованные отличия: общий пароль владельцев, общий внешний
scope у платформенных peers, реальные workspace пути и proxy-aware HTTP(S)
в Landlock. Он не обещает персональную Keycloak identity, namespaces Bubblewrap
или произвольные TCP/UDP/syscalls. Secrets, cookies, DSN, private object keys и
полные ENV dumps в отчёт проверки не включаются.

## Какие logs передать

Сохраните stdout/stderr от начала запуска до готовности либо первого отказа.
`startup.platform` показывает ограниченные факты о helpers, namespaces, cgroups
и TUN; `startup.stage` — стадии configuration, model_configuration, database,
sandbox, runtime, routes, recovery и общий startup. `startup.storage` сообщает
`backend=temporary-posix`, `durable=false` в POSIX trial либо `backend=s3`,
`durable=true` при включённом S3. `application_ready` означает сборку
ASGI application; фактическое открытие listener подтверждают сообщения Uvicorn
и успешные `/health/live`, `/health/ready` через платформу.

При `CORE_AGENT_STARTUP_DIAGNOSTICS=true` в S3-режиме событие
`startup.s3_configuration` показывает фактически выбранные endpoint, bucket и
region, включая fallback из `ARTIFACT_S3_*`. URL очищается от userinfo, query и
fragment; credentials не выводятся, значения ограничены 512 символами. Ошибка
валидации указывает поле и ожидаемый формат, ошибка инициализации SDK — тип
исключения без его содержимого. Эти логи появляются до подключения к S3.

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
