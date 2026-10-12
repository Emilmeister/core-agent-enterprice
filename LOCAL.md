# Локальный запуск

Запусти Docker Desktop и открой терминал в корне репозитория.
На этом Mac `.env` и импорт Keycloak уже подготовлены. Для другого окружения
первоначальная настройка описана в [README](README.md#docker-compose).
Для локального UI нужны `COMPOSE_PROFILES=local-ui` в `.env` и
`deploy/local/core-agent-realm.json`. На Apple Silicon — `SANDBOX_OCI_ARCH=arm64`.

## Запуск

```bash
docker compose up -d
```

Compose запускает агента, PostgreSQL, Keycloak и Phoenix. Контейнеры
`migrate` и `keycloak-init` после успешной инициализации останавливаются — это нормально.

В текущем `.env` порт агента — `8011`:

| Сервис | Адрес |
|---|---|
| Чат | http://localhost:8011/ui/ |
| Keycloak | http://localhost:8085/ |
| Phoenix | http://localhost:6006/ |

Вход в чат: `owner`, пароль — значение `LOCAL_OWNER_PASSWORD` из `.env`.
Вход в админку Keycloak: `admin`, пароль — `LOCAL_KEYCLOAK_ADMIN_PASSWORD`.
Ожидающие подтверждения HITL и guardrails разрешает или отклоняет владелец в чате.

A2A для внешних агентов: `http://localhost:8011/a2a/external/`,
для владельца — `http://localhost:8011/a2a/owner/`.
Для обращения нужны заголовки `Authorization: Bearer <токен>`
и `A2A-Version: 1.0`.

## Состояние и логи

```bash
docker compose ps
docker compose logs -f agent
curl -fsS http://localhost:8011/health/ready
```

## Остановка и обновление

```bash
docker compose down
```

Данные сохраняются в volumes. `docker compose down -v` удаляет их.
После изменения кода пересобери образы: `docker compose up --build -d`.
