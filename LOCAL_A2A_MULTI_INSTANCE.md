# Как поднять два инстанса Core Agent локально и связать их по A2A

Инструкция по настройке **двух параллельных инстансов** одного и того же Core Agent
на одном Mac (Docker Desktop), которые общаются друг с другом по A2A
(`core_agent_send_message`). Оба инстанса живут на одном Docker host и ходят друг
к другу по **docker-compose-алиасам** (`agent-a` / `agent-b`) через общую внешнюю
bridge-сеть `giganet-peers`, по `http`.

> Это дополнение к `LOCAL.md` (первый запуск одного инстанса). Термины
> «инстанс A» и «инстанс B» ниже — условные имена двух копий.

---

## 0. Итоговая картина

| | Инстанс A («основной») | Инстанс B («копия») |
|---|---|---|
| Каталог репозитория | `core-agent-enterprice` | `core-agent-enterprice-b` |
| Сетевое имя в compose | `agent-a` | `agent-b` |
| Хост-порт агента (UI/API) | `8000` | `8100` |
| Хост-порт Keycloak | `8085` | `8185` |
| Тенант компании | `local-company` | `company-b` |
| Issuer Keycloak (в .env) | `http://localhost:8085/realms/core-agent` | `http://localhost:8185/realms/core-agent` |
| Agent Card (внешний вход для соседа) | `http://agent-a:8000/a2a/external/...` | `http://agent-b:8000/a2a/external/...` |

Общая внешняя сеть: `giganet-peers` (bridge, `external: true`). Внутри неё оба
контейнера видят друг друга по именам `agent-a` / `agent-b` и порту 8000.

---

## 1. Копирование репозитория

1. Есть «основное» репо, например:

   ```bash
   git clone <url> core-agent-enterprice
   cd core-agent-enterprice
   git checkout local_a2a
   cp .env.example .env   # затем заполнить .env
   ```

2. Создать **вторую копию** того же репозитория рядом (на другом диске/каталоге):

   ```bash
   git clone <url> core-agent-enterprice-b
   cd core-agent-enterprice-b
   git checkout local_a2a
   cp .env.example .env   # затем заполнить .env
   ```

> Оба репозитория должны содержать **одинаковые** локальные фиксы A2A
> (`remote_agents.py`, `remote_operations.py`, `remote_registry.py`, `tasks.py`,
> `app.py`) и одинаковую ветку `local_a2a`.

---

## 2. Во втором репозитории — отдельные ключи в `.env`

`.env` **никогда не коммитится**. У второй копии почти весь `.env` совпадает с
первой (LLM, база, секреты можно оставить теми же), но **перечисленные ниже
значения должны отличаться**, иначе оба инстанса будут конфликтовать по именам,
портам, тенантам и issuer-ам.

### 2.1. Что обязательно различается

Заполните во втором (`core-agent-enterprice-b`) `.env`:

```dotenv
# Уникальное имя и описание
AGENT_NAME=core-agent-b
AGENT_DESCRIPTION=Policy-enforced core agent runtime (instance B)

# Свой URL — именно по нему сосед (инстанс A) будет вас находить
AGENT_URL=http://agent-b:8000

# Свой тенант компании
CORE_AGENT_TENANT_ID=company-b

# Свой порт Keycloak (чтобы не конфликтовать с 8085 у первого)
KEYCLOAK_ISSUER_URL=http://localhost:8185/realms/core-agent

# Свой хост-порт агента (у первого — 8000)
PORT=8100
```

Остальное (Keycloak `KEYCLOAK_CLIENT_ID=core-agent-api`,
`KEYCLOAK_UI_CLIENT_ID=core-agent-ui`, `LOCAL_OWNER_USERNAME=owner`,
`LOCAL_KEYCLOAK_USERNAME=admin`, LLM, база, `PUSH_NOTIFICATION_ENCRYPTION_KEY`
и т.д.) можно оставить одинаковым для локальной разработки.

### 2.2. Что НЕ надо менять

- `HOST` / внутренний порт контейнера: агент всегда слушает на `8000` внутри
  (`PORT` меняет только опубликованный на хосте порт).
- `KEYCLOAK_CLIENT_SECRET` и encryption key — не критичны для dev, но если хотите
  полную изоляцию, сгенерируйте свои для второго инстанса.

> Набор ключей в обоих `.env` должен быть одинаковым (A и B используют один
> `.env.example`). Меняются только **значения** из списка выше.

---

## 3. Что прописать в docker-compose

Оба `docker-compose.yml` используют один сервисный блок; у второго агента меняются **только порты Keycloak и алиас сети**. Убедитесь, что в
`core-agent-enterprice-b/docker-compose.yml`:

### 3.1. Keycloak — свой порт (у B: 8185)

```yaml
keycloak:
  ...
  environment:
    ...
    KC_HTTP_PORT: "8185"                 # у A было "8085"
    KC_HOSTNAME: http://localhost:8185   # у A было http://localhost:8085
```

### 3.2. Agent — публикуем свой Keycloak-порт на хосте

```yaml
agent:
  ...
  ports:
    - "127.0.0.1:${PORT:-8000}:8000"     # у B PORT=8100 → 127.0.0.1:8100:8000
    - "127.0.0.1:8185:8185"              # у A было "127.0.0.1:8085:8085"
```

### 3.3. Сеть `giganet-peers` и алиас

В обоих compose **один и тот же** внешний блок (общая сеть на один Docker host):

```yaml
agent:
  ...
  networks:
    default:
    giganet-peers:
      aliases:
        - agent-b       # у A здесь agent-a

volumes:
  postgres-data:
  agent-data:
  keycloak-data:

networks:
  giganet-peers:
    external: true
    name: giganet-peers
```

> Обратите внимание: у сервиса `keycloak` используется
> `network_mode: service:agent`, поэтому Keycloak публикуется через порты агента —
> отсюда и пункт 3.2.

### 3.4. Предварительно создать общую сеть (один раз)

Оба compose-файла ссылаются на `giganet-peers` как на `external`, поэтому сеть должна
существовать **до** первого запуска:

```bash
docker network create giganet-peers
```

Создаётся один раз и на одном хосте используется обоими инстансами.

---

## 4. Команды для запуска

Порядок важен: сначала общая сеть, затем оба инстанса (любой порядок, но оба
должны быть подняты, чтобы `core_agent_send_message` увидел соседа).

```bash
# 0. Общая внешняя сеть (запускаем один раз)
docker network create giganet-peers

# 1. Инстанс A
cd core-agent-enterprice
docker compose up -d --build agent
docker compose up -d keycloak      # профиль local-ui; keycloak-init поднимется сам

# 2. Инстанс B
cd ../core-agent-enterprice-b
docker compose up -d --build agent
docker compose up -d keycloak
```

Для наблюдения:

```bash
docker ps                         
docker logs -f core-agent-enterprice-agent-1
docker logs -f core-agent-enterprice-b-agent-1
```

Адреса UI:
- Инстанс A: `http://localhost:8000/ui/`
- Инстанс B: `http://localhost:8100/ui/`

Проверка, что агенты видят друг друга (Agent Card доступна по сети):

```bash
# из A: карточка B
curl -H "A2A-Version: 1.0" http://agent-b:8000/a2a/external/.well-known/agent-card.json
# из B: карточка A
curl -H "A2A-Version: 1.0" http://agent-a:8000/a2a/external/.well-known/agent-card.json
```

(Команды `curl` изнутри контейнера самого алиаса могут не пройти — проще
проверять из UI/логов после того, как сосед зарегистрирован.)

---

## 5. Что потом сделать в UI

A2A работает через **реестр пиров компании** (owner UI) и **external service accounts** в Keycloak соседа. Суть: каждый инстанс добавляет другого как
«доверенного remote peer» и даёт ему токен доступа (внешний service account).

1. **Зарегистрировать пира B в A** (owner UI инстанса A → управление remote
   peers / remote agents). Добавьте:
   - Name: `agent-b`
   - URL: `http://agent-b:8000/a2a/external`
   - Enabled: включён
   - Credential: Bearer-токен **внешнего service account из Keycloak инстанса B**
     (формат: строка, начинается с `Bearer …`).

2. **Зарегистрировать пира A в B** (owner UI инстанса B):
   - Name: `agent-a`
   - URL: `http://agent-a:8000/a2a/external`
   - Enabled: включён
   - Credential: Bearer-токен **внешнего service account из Keycloak инстанса A**.

3. **Токены доступа.** Между инстансами в Keycloak соседа создаются
   external service accounts (owner-стороны):
   - из B, чтобы A мог ходить в B: в Keycloak **B** создаётся external service
     account для владельца A, его токен идёт в реестр A как секрет пира `agent-b`;
   - симметрично для B -> A. Точные имена сущностей в Keycloak Admin (realm каждого
     инстанса) могут отличаться, но принцип — взаимная выдача external
     credentials.

4. **Проверить, что инструмент виден.** После того как у инстанса есть хотя бы
   один включённый peer, в каталоге появляется `core_agent_send_message`. В
   owner UI в списке сообщений должен быть виден peer.

5. **Пинг.** В чате инстанса A попросите: «поприветствуй агента B», либо
   «отправь агенту B: сколько будет 5−4?». Затем то же зеркально из B. В логах
   того и другого должны появиться входящие
   `POST /a2a/external/message%3Asend … 200` и события `remote.pinned`,
   `remote.admitted`, `task.completed` — это означает, что A2A-обмен реально
   прошёл.

---

## Диагностика

- `workflow.recovery.failed` + `CHECKPOINT_INVALID` в цикле по
  `core_agent_send_message` -> почти всегда локальный пир не проходит строгую
  валидацию url (`_remote_contract`/`_trusted_endpoint`). Нужна ветка `local_a2a`
  с фиксами `remote_registry`/`remote_agents`/`tasks.py`.
- Нет `core_agent_send_message` в каталоге -> peer не включён либо
  `connect_peer` не смог прочитать Agent Card соседа (проверьте url
  `…/a2a/external` и правильность Bearer-токена).
- Оба инстанса «здоровы», но сообщения не доходят -> проверьте, что оба контейнера
  в одной сети `giganet-peers` (`docker network inspect giganet-peers`) и что
  внешние service accounts взаимно выданы.

### Ловушка `network_mode: service:` при пересоздании `agent`

Контейнеры, объявленные с `network_mode: service:agent` (Keycloak, а также любые
локальные MCP-серверы в namespace агента, см. раздел про MCP ниже), привязываются
к **container-id** того `agent`, который существовал в момент их создания. Compose
не пересоздаёт их автоматически, когда вы пересоздаёте сам `agent` (например
`docker compose up -d --no-deps keycloak` или `up -d --force-recreate agent`):
они продолжают ссылаться на **удалённый** старый container-id.

**Симптом:** сами контейнеры `Up`/`healthy`, агент `healthy`, UI/Keycloak работают,
но MCP-серверы в namespace агента «не отвечают» — discovery агентом не находит
тулы, blockchain-MCP отдаёт `/health` = **503 Service Unavailable** (его внешний
DNS `bb.blch.ru` не резолвится из «обезглавленного» namespace,
`NameResolutionError ... Temporary failure in name resolution`).

**Проверка (должно указывать на ЖИВОЙ agent-id):**

```bash
docker inspect <agent>-agent-1 --format '{{.Id}}'
docker inspect <mcp-name>-1 --format '{{.HostConfig.NetworkMode}}'   # = container:<agent-id>
```

`error: no such object` на этом container-id — подтверждение, что ссылка устарела.

**Исправление:** заставить compose пересоздать зависимые контейнеры, чтобы они
переподключились на текущий agent-id (контейнеры stateless, пересоздание
безопасно):

```bash
cd core-agent-enterprice
docker compose up -d --force-recreate keycloak <mcp-service-1> <mcp-service-2>
```

После пересоздания `/health` MCP-серверов снова 200, а в логах агента discovery
показывает `"connected": true` для всех MCP-серверов и полный набор
discovered/allowed-тулов.

### Локальные MCP-серверы в namespace агента

Ядро разрешает plain-HTTP MCP **только для loopback**; `host.docker.internal`
loopback'ом не является и на connect отклоняется. Поэтому локальные MCP-серверы
(как Keycloak) объявляются с `network_mode: service:agent` и слушают на
`127.0.0.1:<внутренний порт>`, а в `.env` агента указывается
`MCP_URL=http://127.0.0.1:<порт>/mcp`. Порты таких серверов в host-сеть не
публикуются — проверять их health нужно через `/health` самого сервера из
namespace агента либо через compose healthcheck, а не с хоста
(`000`/connection refused с хоста — это норма, а не поломка).

`MCP_ALLOWED_SERVERS` и `MCP_ALLOWED_TOOLS` (scoped `server.tool`) заполняются в
`.env`; без явного списка `MCP_ALLOWED_TOOLS` ядро не отдаёт модели ни один
MCP-тул (`allowed: []`, «агент копается в памяти»).
