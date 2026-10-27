# Два локальных Core Agent, связанных по A2A

Стенд для одного компьютера с Docker Desktop: два инстанса Core Agent — покупатель (A) и поставщик (B). Каждый со своими MCP-серверами ERP и блокчейна GN. Агенты передают друг другу задачи тулом `core_agent_send_message` по http через общую Docker-сеть `giganet-peers`.

Все команды ниже запускаются из каталога, указанного в начале блока. Используется только `docker-compose.local.yml`: это самодостаточный файл со всем стеком (база, миграции, Phoenix, Keycloak, агент, MCP-серверы). `docker-compose.yml` для стенда не нужен.

## 1. Что где

| | A — покупатель | B — поставщик |
|---|---|---|
| Каталог | `core-agent-enterprice` | `core-agent-enterprice-b` |
| Имя в сети `giganet-peers` | `agent-a` | `agent-b` |
| UI и API | http://localhost:8000/ui/ | http://localhost:8100/ui/ |
| Keycloak | http://localhost:8085 | http://localhost:8185 |
| Тенант | `local-company` | `company-b` |
| MCP ERP (сервис / образ) | `fosagro-mcp` / `fosagro-mock-mcp:local`, порт 8012 | `etm-mcp` / `etm-mock-mcp:local`, порт 8011 |
| MCP блокчейна (сервис / образ) | `blockchain-buyer` / `amity-mcp:local`, порт 8022 | `blockchain-seller` / `amity-mcp:local`, порт 8023 |

Рядом должны лежать репозитории MCP-серверов: `fosagro-mock-mcp`, `etm-mock-mcp`, `blockchain-mcp`.

MCP-серверы работают в сетевом пространстве контейнера агента (`network_mode: service:agent`) и слушают на `127.0.0.1`. Ядро разрешает MCP по plain http только на loopback. Порты MCP на хост не публикуются, поэтому запрос к ним с хоста даёт `connection refused` — это нормально.

## 2. Один раз: сеть и образы MCP

```bash
docker network create giganet-peers
```

Образы MCP-серверов собираются из их репозиториев:

```bash
cd fosagro-mock-mcp && docker build -t fosagro-mock-mcp:local . && cd ..
cd etm-mock-mcp     && docker build -t etm-mock-mcp:local .     && cd ..
cd blockchain-mcp   && docker build --platform linux/amd64 -t amity-mcp:local . && cd ..
```

После изменений в коде MCP-сервера пересоберите его образ и перезапустите инстанс (раздел 5).

## 3. `.env` каждого инстанса

`.env` не коммитится. Создайте его из примера в каждом каталоге: `cp .env.example .env`, заполните LLM, базу и секреты. Дальше — значения, которые нужны стенду.

### Общие для A и B

```dotenv
BLOCKCHAIN_GATE_URL=<адрес gate GN>
```

### Только A

```dotenv
AGENT_NAME=core-agent-a
AGENT_URL=http://agent-a:8000
CORE_AGENT_TENANT_ID=local-company
KEYCLOAK_ISSUER_URL=http://localhost:8085/realms/core-agent
PORT=8000

WALLET_CRYPTO_BUYER=<кошелёк покупателя в GN>
MEMBER_ID_BUYER=<member id покупателя в GN>

MCP_URL=http://127.0.0.1:8012/mcp,http://127.0.0.1:8022/blockchain
MCP_ALLOWED_TOOLS=mcp.search_supplier_catalog_items,mcp.create_purchase_request,mcp.get_purchase_request,mcp.record_supplier_order,mcp.get_supplier_order,mcp.confirm_delivery_received,blockchain.amity_status,blockchain.amity_contracts,blockchain.amity_agents,blockchain.amity_members,blockchain.amity_tokens,blockchain.amity_datafeeds,blockchain.amity_create_contract,blockchain.amity_fund_contract,blockchain.amity_submit_payment_order,blockchain.amity_submit_delivery_confirmed
```

### Только B

```dotenv
AGENT_NAME=core-agent-b
AGENT_URL=http://agent-b:8000
CORE_AGENT_TENANT_ID=company-b
KEYCLOAK_ISSUER_URL=http://localhost:8185/realms/core-agent
PORT=8100

WALLET_CRYPTO_SELLER=<кошелёк поставщика в GN>
MEMBER_ID_SELLER=<member id поставщика в GN>

MCP_URL=http://127.0.0.1:8011/mcp,http://127.0.0.1:8023/blockchain
MCP_ALLOWED_TOOLS=mcp.search_products,mcp.get_product_card,mcp.get_product_price,mcp.get_product_remains,mcp.calculate_order,mcp.create_order,mcp.get_order_status,mcp.get_order_composition,mcp.get_order_specification,mcp.create_delivery_request,mcp.add_order_comment,mcp.list_order_comments,blockchain.amity_status,blockchain.amity_contracts,blockchain.amity_agents,blockchain.amity_members,blockchain.amity_tokens,blockchain.amity_datafeeds,blockchain.amity_submit_shipment_done
```

Как это читать:
- Имя MCP-сервера — последний сегмент пути в `MCP_URL`: `/mcp` → `mcp`, `/blockchain` → `blockchain`. Модель видит тулы с этим префиксом: `mcp_create_order`, `blockchain_amity_contracts`.
- В `MCP_ALLOWED_TOOLS` перечисляются все тулы, которые получает модель, в виде `сервер.тул`. Тул не из списка модель не увидит. Шаблонов (`*`) нет.
- Банковские тулы (`amity_submit_bank_success`, `amity_submit_bank_rejected`) в списки не входят: эти события публикует банк, а не агенты.
- Константы GN (шаблон контракта, фиды, тип токена) заданы в `docker-compose.local.yml` у сервисов блокчейна, в `.env` их нет.

## 4. Первый запуск

```bash
cd core-agent-enterprice
docker compose -f docker-compose.local.yml --profile local-ui up -d --build

cd ../core-agent-enterprice-b
docker compose -f docker-compose.local.yml --profile local-ui up -d --build
```

`--profile local-ui` включает Keycloak и его начальную настройку (`keycloak-init`). Миграции базы выполняются сами.

Готовность: `curl -s localhost:8000/health/ready` и `curl -s localhost:8100/health/ready`.

## 5. Пересборка и перезапуск

```bash
# A (core-agent-enterprice)
docker compose -f docker-compose.local.yml build agent
docker compose -f docker-compose.local.yml up -d --force-recreate agent keycloak fosagro-mcp blockchain-buyer

# B (core-agent-enterprice-b)
docker compose -f docker-compose.local.yml build agent
docker compose -f docker-compose.local.yml up -d --force-recreate agent keycloak etm-mcp blockchain-seller
```

Агента перезапускайте **только вместе** с Keycloak и MCP-серверами своего инстанса. Они работают в сетевом пространстве контейнера агента и привязаны к конкретному контейнеру. Если пересоздать один `agent`, они останутся привязаны к удалённому контейнеру: агент не увидит MCP-тулов, а авторизация соседа сломается.

## 6. Связать A и B (один раз, в UI)

Каждый инстанс выдаёт соседу токен доступа, а сосед регистрирует его как внешнего агента.

1. В UI **B** (http://localhost:8100/ui/) → «Доступ к агенту» → «Выдать доступ». Создайте учётку для A и скопируйте токен («Скопировать токен»).
2. В UI **A** (http://localhost:8000/ui/) → «Внешние агенты» → «Добавить агента»:
   - имя: `agent-b`;
   - адрес: `http://agent-b:8000/a2a/external`;
   - секрет: `Bearer <токен из шага 1>`.
3. Зеркально: в UI **A** выдайте доступ для B, в UI **B** добавьте агента `agent-a` с адресом `http://agent-a:8000/a2a/external`.

Проверка: в чате A напишите «спроси у agent-b, сколько будет 5−4». В логах B появится `POST /a2a/external/message%3Asend … 200`, в чате A — ответ B.

## 7. Логи

```bash
docker compose -f docker-compose.local.yml logs --no-color --since 30m agent
```

Полезные события: `model.response` (что ответила модель и какие тулы вызвала), `tool.failed`, `next_steps.ignored`, `workflow.recovery.failed` и `task.exception` (в поле `error_message` — ответ провайдера модели), `workflow.recovery.delayed`.

## 8. Если что-то не работает

- **Модель не видит MCP-тулы.** Проверьте `MCP_ALLOWED_TOOLS` и что агент перезапущен вместе со своими MCP-серверами (раздел 5). В логе агента событие `capabilities.resolved` показывает, к каким MCP удалось подключиться (`"connected": true`) и какие тулы разрешены.
- **Нет тула `core_agent_send_message`.** У инстанса нет ни одного включённого внешнего агента, либо не удалось прочитать карточку соседа: проверьте адрес `…/a2a/external` и токен.
- **Сообщения не доходят до соседа.** Оба контейнера агентов должны быть в сети `giganet-peers`: `docker network inspect giganet-peers`.
