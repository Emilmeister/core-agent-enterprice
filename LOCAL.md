# Локальное развёртывание на macOS

## Требования

Установленный и запущенный Docker Desktop с Docker Compose. Для виртуальной
машины Docker требуется не менее 8 ГБ оперативной памяти. Исходный код проекта
должен находиться в локальном каталоге; все команды выполняются из его корня.
Python, Node.js и Kubernetes на хосте не требуются.

## Подготовка конфигурации

При первом развёртывании создайте файл конфигурации:

```bash
cp .env.example .env
chmod 600 .env
```

В `.env` задайте следующие значения:

```dotenv
COMPOSE_PROFILES=local-ui
CORE_AGENT_ENVIRONMENT=development
PORT=8000
AGENT_URL=http://localhost:8000
KEYCLOAK_ISSUER_URL=http://localhost:8085/realms/core-agent
KEYCLOAK_CLIENT_ID=core-agent-api
KEYCLOAK_UI_CLIENT_ID=core-agent-ui
KEYCLOAK_AUDIENCE=core-agent-api
CORE_AGENT_TENANT_ID=local-company
LOCAL_OWNER_USERNAME=owner
LOCAL_KEYCLOAK_USERNAME=admin
SANDBOX_DNS_SERVERS=1.1.1.1
SANDBOX_DENIED_CIDRS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7
```

Для Apple Silicon укажите `SANDBOX_OCI_ARCH=arm64`, для Intel — `amd64`.
Заполните `LLM_API_BASE`, `LLM_MODEL` и `LLM_API_KEY` данными провайдера.
Шаблон использует OpenAI-совместимый API (`LLM_API_FORMAT=openai`).

Задайте отдельные секреты для `POSTGRES_PASSWORD`, `KEYCLOAK_CLIENT_SECRET`,
`LOCAL_OWNER_PASSWORD` и `LOCAL_KEYCLOAK_ADMIN_PASSWORD`. Для генерации каждого
значения можно использовать:

```bash
openssl rand -hex 32
```

Для `PUSH_NOTIFICATION_ENCRYPTION_KEY` сгенерируйте ключ отдельной командой
и сохраните её результат в `.env`:

```bash
openssl rand -base64 32 | tr '+/' '-_'
```

Остальные настройки можно оставить из шаблона. Файл `.env` содержит секреты;
его нельзя публиковать. Ключ шифрования необходимо сохранять между запусками.

## Запуск и вход

```bash
docker compose up -d
```

При первом запуске Compose собирает образы, создаёт схему БД, настраивает
Keycloak и учётную запись владельца. Дополнительные скрипты запускать не требуется.
Контейнеры `migrate` и `keycloak-init` завершаются после успешной инициализации.

| Сервис | Адрес | Учётная запись |
|---|---|---|
| Чат | http://localhost:8000/ui/ | `LOCAL_OWNER_USERNAME` / `LOCAL_OWNER_PASSWORD` |
| Keycloak | http://localhost:8085/ | `LOCAL_KEYCLOAK_USERNAME` / `LOCAL_KEYCLOAK_ADMIN_PASSWORD` |
| Phoenix | http://localhost:6006/ | Локальный доступ без авторизации |

Все порты доступны только с локального компьютера. При изменении `PORT`
до первого запуска также измените `AGENT_URL`. Существующие пароли и настройки
клиентов Keycloak изменяются через его административный интерфейс.

A2A-эндпоинты: `/a2a/external/` для внешних агентов, `/a2a/owner/` для владельца.
Запросы требуют заголовков `Authorization: Bearer <токен>` и `A2A-Version: 1.0`.
Токен внешнему агенту выдаётся владельцем во вкладке «Доступ к агенту».

## Проверка, остановка и обновление

```bash
docker compose ps
curl -fsS http://localhost:8000/health/ready
docker compose logs -f agent
```

Остановка: `docker compose down`. Данные сохраняются в Docker volumes.
Команда `docker compose down -v` удаляет БД, файлы чатов и данные Keycloak.
После изменения исходного кода: `docker compose up --build -d`.
