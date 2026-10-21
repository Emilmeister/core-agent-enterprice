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
  deployment. Sandbox требует Linux namespaces, seccomp, TUN и resource limits;
  параметры и outer security profile описаны в [инструкции](../deploy/kubernetes/apparmor/README.md)
  и [Pod fixture](../deploy/kubernetes/sandbox-pod.yaml).

MCP настраиваются в owner UI. Автоматические `MCP_URL`/`MCP_HEADERS_JSON` здесь
игнорируются. Platform `ARTIFACT_STORAGE_*`, `ARTIFACT_S3_*`, `ARTIFACT_MONGODB_*`
и `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS` тоже игнорируются: они не включают S3,
не импортируют прежние blobs и не мешают диагностическому запуску. Default
Keycloak profile продолжает отклонять эти retired artifact settings.

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
Для внешнего peer используйте `<AGENT_URL>/a2a/external/` и отдельный статический
Cloud API key; выдача/отзыв выполняются вручную в Cloud console. Peers имеют общий
external scope для известных Task IDs; перечисление Tasks закрыто, owner данные
им недоступны.

## Какие logs передать

Сохраните stdout/stderr от начала запуска до готовности либо первого отказа.
`startup.platform` показывает ограниченные факты о helpers, namespaces, cgroups
и TUN; `startup.stage` — стадии configuration, model_configuration, database,
sandbox, runtime, routes, recovery и общий startup. `startup.storage` сообщает
`backend=temporary-posix`, `durable=false`. `application_ready` означает сборку
ASGI application; фактическое открытие listener подтверждают сообщения Uvicorn
и успешные `/health/live`, `/health/ready` через платформу.

Перед штатной проверкой sandbox записи `startup.sandbox_probe` отдельно показывают
создание user namespace, отображение UID/GID и доступность mount/PID/IPC/network/UTS
namespaces. Затем выполняются три фиксированных запуска Bubblewrap с `/usr/bin/true`:
минимальный, с `/proc` и со всеми namespaces, `/proc`, `/dev`, временным `/tmp`.
Они не используют TUN, код модели или ENV приложения. Это позволяет отделить
отказ создания namespace от отказа монтирования `/proc`; отсутствие TUN относится
к последующей проверке полного сетевого профиля.

Выполнение одной проверки занимает не более 2 секунд, общий бюджет запуска
проверок — 10 секунд; на остановку её процессов отдельно отводится до 1 секунды.
В logs остаются status/reason и cleanup, а operation/errno/returncode добавляются,
когда доступны. Stderr ограничен 4096 байтами и целиком не публикуется. Отказы видны и при
`LOG_LEVEL=CRITICAL`. Если остановка диагностического процесса не подтверждена,
startup прекращается. `EPERM` сам по себе не определяет виновника: AppArmor,
seccomp и другие ограничения могут вернуть одинаковую ошибку.

Диагностика выводит конечные status/reason-коды и наличие конфигурации без её
значений. Не прикладывайте ENV dump, пароли, DSN, tokens или содержимое запросов.
При отказе sandbox передайте записи `startup.sandbox_probe` и stage/reason
штатного preflight. Успех отдельных probes не означает готовность sandbox:
обязательная полная проверка сохраняется. Локальные tests и успешная сборка
образа не заменяют эту проверку реального Pod.
