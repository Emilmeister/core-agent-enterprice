# Keycloak и A2A access — план первого runtime среза

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Защитить входящие HTTP-запросы через Keycloak и разграничить владельцев и внешние service accounts.

**Architecture:** Один ASGI middleware проверяет introspection перед dispatch. Два A2A mount используют тот же handler и stores; доверенный context builder передаёт company scope и стабильную identity. Task stores сохраняют владение независимо от токена и дают владельцам доступ ко всем задачам своей компании.

**Tech Stack:** Существующие httpx, Starlette, A2A SDK, PostgreSQL и unittest; без новой зависимости.

## Файлы и контракт

- Создать `core_agent/auth.py`: validated deployment settings, introspection,
  verified principal, ASGI gate и SDK context builder.
- Изменить `core_agent/a2a_sdk.py`: mount-aware Agent Card; in-memory task store
  с тем же owner/company доступом, что у PostgreSQL.
- Изменить `core_agent/database.py`: owner-role чтение/изменение задач в своей
  company, сохранение исходного owner при действиях владельца.
- Изменить `core_agent/app.py`: подключить auth, два входа и identity endpoint,
  сохранить один lifecycle/recovery loop.
- Создать `tests/test_auth.py`: реальные HTTP boundary проверки с подменой только
  Keycloak HTTP transport; настоящие task stores/model loop для сквозных случаев.
- Изменить `.env.example`, `README.md`, `AGENTS.md` в пределах нового контракта.

Входы: `/a2a/owner`, `/a2a/external`; `/api/identity` только для владельцев.
`/api/hitl` резервируется в нормативном контракте для следующего среза, но
пустой handler для него не добавляется. Health endpoints доступны probes.

Настройки: `KEYCLOAK_ISSUER_URL`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET`,
`KEYCLOAK_AUDIENCE`, `KEYCLOAK_OWNER_ROLE=agent-owner`,
`KEYCLOAK_EXTERNAL_ROLE=agent-external`, `CORE_AGENT_TENANT_ID`.
Production требует полного набора; отсутствие всей auth-конфигурации допускает
старый вход только в явно выбранном development/test. Сам
`CORE_AGENT_ENVIRONMENT` обязателен; неизвестное значение — ошибка старта.
Частичная auth-конфигурация — ошибка старта.

Introspection идёт по фиксированному issuer endpoint через confidential client,
без redirect, без HTTP proxy из окружения, с ограниченным timeout/response size.
Проверяются active, issuer, audience, subject, expiry и not-before. Роли берутся
только из realm roles и roles ожидаемого audience. External role исключает
owner-права даже при одновременно выданной owner role.

Company берётся из deployment config; внешний owner ID выводится из issuer/sub,
а не token/client-supplied metadata. Владельцы используют общий scope для новых
задач, actor identity сохраняется отдельно. Чтение/изменение чужой задачи
владельцем не меняет её owner и доступ исходного внешнего caller.

## Последовательность и проверки

- [x] Убедиться, что описанная семантика перенесена в нормативные документы.
- [x] Добавить failing HTTP тест: без Bearer нет допуска к model loop;
  валидный owner получает доступ, external не получает owner API.
- [x] Реализовать introspection/middleware/context builder и подключить их
  в `create_app`; старый публичный маршрут при настроенном auth не доступен.
- [x] Проверить два владельца, два внешних caller, replacement token с тем же
  sub, неверный audience/issuer, malformed claims, expired/revoked token,
  недоступный Keycloak и повторный запрос в одном HTTP client.
- [x] Проверить, что stream не вызывает повторную introspection на каждый кадр,
  а новая подписка проходит новую проверку; ошибки не содержат token/secret.
- [x] Проверить scoped Get/List/Cancel и owner доступ на обоих stores; изменение
  owner-ом задачи не переносит её в другой scope.
- [x] Проверить состав Agent Card и URL обоих mount, включая proxy base URL.
- [x] Проверить, что входящий Bearer не попадает в model context, persisted state
  или исходящий remote-agent header.
- [x] Выполнить targeted suite, затем Ruff и полный suite с PostgreSQL.

```bash
uv run python -m unittest tests.test_auth -v
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
```

Для последних двух тестовых команд передаётся локальный `TEST_DATABASE_URL`.
Результат интеграции с настоящим Keycloak проверяется отдельно от HTTP заглушки;
долгий срок токена задаётся Keycloak, приложение не выпускает собственные ключи.

## Доказательства первого среза

HTTP regressions выполняются и с памятью, и с PostgreSQL. Дополнительно
`tests.test_keycloak_integration` создаёт отдельный временный realm настоящего
Keycloak, выдаёт service-account токен с годовым сроком, заменяет его и проверяет
отказ после отключения client. В CI закреплён Keycloak 26.1.4 по digest; его
Access Token Lifespan ограничивается SSO Session Max, оба настроены в fixture.

Полный suite с pgvector/PostgreSQL и Keycloak: 437 tests, без пропусков, exit 0.
Ruff и Compose config: exit 0. Docker image построен; non-root/import/startup
smoke включены в проверку среза. Review выявил и закрыл два обхода: SDK active
cancel теперь сначала проверяет scoped store, а legacy режим требует явно
выбранного development/test. Admission audit и follow-up provenance сохраняют
отдельный actor_id при общем owner scope.

Owner UI, HITL endpoint, file/chat isolation и новая chat admission семантика
остаются следующими этапами, поэтому ENT-AC-01/02/03 ещё не implemented.
