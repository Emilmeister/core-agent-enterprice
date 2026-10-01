# Enterprise owner UI — implementation plan

> **For agentic workers:** use `subagent-driven-development` to execute the approved enterprise scope in reviewable steps. This plan does not supersede normative `spec/` or authorize unrelated work.

**Goal:** Общий интерфейс владельцев: чаты, решения HITL/guardrails, вопросы,
файлы, доверенные агенты, расписания и настройки.

**Architecture:** React/TypeScript/Vite собирается в статические файлы,
которые отдаёт существующий ASGI service. Keycloak JavaScript adapter выполняет
Authorization Code + PKCE S256. UI использует существующий owner A2A lifecycle
и owner API; браузер не исполняет, не планирует и не возобновляет workflow сам.

**Tech stack:** согласованные React, TypeScript, Vite, обычный CSS;
официальный `keycloak-js`. Без UI framework, state framework или отдельного
backend-for-frontend. Dependency versions фиксируются lockfile после реального
install/build; не угадывать версии и не коммитить `node_modules`.

## Источники и границы

- `spec/product.md` UI-01, `spec/agent-configuration.md` UI-02,
  `spec/public-contract.md` owner API, `spec/security-and-reliability.md`.
- [Keycloak JavaScript adapter](https://www.keycloak.org/securing-apps/javascript-adapter):
  public browser client, точные redirect URIs/Web Origins, refresh перед HTTP,
  access/refresh tokens только в памяти. Confidential introspection credential
  приложения в браузер не передаётся.
- [React + build tool](https://react.dev/learn/build-a-react-app-from-scratch)
  и [Vite guide](https://vite.dev/guide/) — сборка SPA; SSR здесь не требуется.

До кодирования соответствующего API уточнить нормативный контракт в существующих
spec и acceptance/hash lock в пределах уже разрешённых enterprise изменений.
Этот документ описывает реализацию согласованной возможности, не новую модель
авторизации. Существующие endpoints не объявляются готовыми только из-за наличия
UI-кнопки.

## Визуальная структура

Визуальный ориентир по прямому уточнению пользователя — привычный UI ChatGPT:
нейтральная светлая поверхность, графитовый текст, компактная левая колонка
чатов, центрированная лента и округлое поле сообщения снизу. User messages справа,
ответы агента без dashboard card рамок. Цвет служит статусам и ожиданиям.
Раскрываемая панель файлов справа. Дата, статус задачи и
источник чата читаются без технического JSON. Детали вызова инструмента
показываются по запросу рядом с решением. Приватное содержимое выводится текстом;
никакого raw HTML из модели, tool result или имени файла.

Навигация: «Чаты», «Инструменты», «Агенты», «Расписания», «Настройки».
Отдельной общей очереди HITL нет. Вопросы и решения встроены в нужную ленту,
сохраняют исходный deadline и принятый outcome. Два владельца видят один и тот
же результат; устаревшая вкладка не перезаписывает чужое решение.

Для небольших экранов список чатов скрывается за доступной кнопкой, а файловая
панель становится отдельным экраном. Все поля имеют labels, клавиатурный focus,
контраст и понятные inline ошибки; reduced motion учитывается. Никакие шрифты,
аналитика или скрипты не загружаются с третьих доменов при работе приложения.

## 1. Браузерный вход и отдача сборки

**Files:** `ui/package.json`, `ui/package-lock.json`, `ui/index.html`,
`ui/tsconfig.json`, `ui/vite.config.ts`, `ui/src/auth.ts`, `ui/src/main.tsx`,
`core_agent/app.py`, `core_agent/auth.py`, `.env.example`, `Dockerfile`,
`tests/test_auth.py`, `AGENTS.md` и профильные spec.

- [x] Зафиксировать `KEYCLOAK_UI_CLIENT_ID` как отдельный public client и публичный
  bootstrap route с только issuer/client ID. Browser flow не использует secret
  confidential client. Никаких credentials в build-time `VITE_*`.
  Backend `/ui/config` реализован: exact public path, issuer/client_id only,
  query400, disabled404, no-store/nosniff/referrer/CSP. Owner/A2A routes закрыты
  без bearer. Gate auth/owner_chats/spec:39 tests,25 executed,14 PostgreSQL skips,
  exit0; Ruff проходит. Review выявил stripping env перед validation; исправлено
  адресно для browser ID, composition regression отклоняет неверное значение до
  выделения runtime. Independent re-review:5 tests executed,exit0, замечаний нет.
  Переменная проброшена в Compose. Это bootstrap, actual SPA/login ещё не реализованы.
- [x] Публичны только shell/assets/bootstrap. Owner API, A2A и material/file
  reads сохраняют server-side authorization. SPA fallback не перехватывает
  неизвестный `/api/` или `/a2a/` route и не превращает 401/404 в HTML.
  `ui_routes` подключён к actual composition root; enumeration регулярных assets,
  exact GET/HEAD auth allowlist, symlink/missing-path checks и headers проверены
  synthetic build/ASGI tests. Static/auth gate:29 tests,20 executed,9 PostgreSQL
  skips,exit0. Independent review не нашёл blockers. Это не actual SPA build.
- [ ] Инициализировать Keycloak до routing, code flow + S256, проверять
  `/api/identity` после login. External/dual-role account получает отказ.
  Refresh перед новым запросом; failed refresh очищает private UI state.
  Access/refresh tokens не сохранять в localStorage/sessionStorage, URL или логи.
- [ ] Добавить подходящий CSP, nosniff, referrer policy и no-store для bootstrap
  и приватных ответов. Connect-src разрешает только same-origin и configured
  Keycloak origin. Не разрешать произвольный runtime URL из query.
- [ ] Vite production build копируется отдельным Docker build stage; конечный
  образ не получает npm/node build tools. Обновить .dockerignore/.gitignore.
- [ ] Проверить build и auth/static routing через HTTP; затем реальный браузер
  с тестовым Keycloak realm. Mock login не считать Keycloak proof.

## 2. Чаты, lifecycle и решения

**Files:** `ui/src/api.ts`, `ui/src/App.tsx`, `ui/src/Chat.tsx`,
`ui/src/Interactions.tsx`, `ui/src/styles.css`; только недостающие backend
chat/history routes в существующей owner API boundary после определения spec.

- [ ] Использовать `/a2a/owner/` и bearer header. Точные SDK JSON types,
  pagination/query names и stream event shape взять из установленного SDK и
  минимум двух существующих HTTP тестов, не придумывать wire contract.
- [ ] Общий список чатов отражает durable chat/context identity, все страницы
  и полную историю. Не приравнивать одну Task к одному чату и не получать
  «полную историю» из ограниченного task snapshot или model summary.
- [ ] Отправка в active Task — follow-up с `taskId`; в idle/terminal чате —
  новая Task с прежним `contextId`. messageId создаётся один раз на отправку.
  При неоднозначной сети повторяет тот же messageId/payload по явному действию;
  новый ID не создаётся автоматически. Busy error показывает активную Task.
- [ ] Для прогресса использовать авторизованный fetch-stream, поскольку
  EventSource не передаёт bearer header. Reconnect сначала читает persisted
  Task, затем подписывается; не запускает новую задачу. Abort fetch при уходе
  со страницы не вызывает task cancel; отмена — отдельное действие пользователя.
- [ ] Внутри чата читать `/api/interactions?task_id=...` с pagination.
  HITL: только разрешить/отклонить; вопрос: ответить; guardrail: owner-only
  просмотр материала и разрешить/отклонить. subject_digest передавать неизменным.
  При 409 перечитать актуальное состояние, не посылать автоматическое решение.
- [ ] Полный E2E: два owner browser contexts, external-owned task, pending
  approval, owner decision, browser reload/reconnect, terminal result и отсутствие
  private Q/A в external A2A. Browser закрыт во время ожидания — runtime продолжает.

## 3. Настройки инструментов и ожиданий

**Files:** `ui/src/Settings.tsx`, `ui/src/ToolPolicies.tsx`, текущие owner API.

- [ ] Три режима каждого tool и отдельный guardrails exemption используют
  origin/revision CAS. Показывать смысл исключения для args/results и сохранение
  HITL. Не давать включить capability за platform ceiling.
- [ ] Timeout формы используют серверные имена полей и единицы; не сбрасывают
  deadline уже созданного ожидания. Conflict обновляет форму и сообщает о другом
  изменении, не перезаписывает его. Не изменять policy при простом открытии формы.
- [ ] Проверить deny → исчезновение tool из нового model catalog, существующий
  HITL при переключении allow, два владельца с конфликтующими revision.

## 4. Файлы, внешние агенты и cron

**Dependencies:** реальные API из files/interactions/remote/cron implementation
slices. UI не создаёт временные несовместимые backend store или task state machine.

- [ ] File picker показывает полный набор, decoded aggregate size и текущий
  server limit. Whole-message rejection оставляет draft и файлы для исправления.
  Accepted receipt показывает фактические имена/папку. Download запрашивает
  owner-authenticated scoped bytes, не доверяет URL из ответа модели.
- [ ] Workspace: возрастной фильтр, ручной выбор, серверный preview и окончательное
  подтверждение только показанного revision-bound набора. Gone/stale/active-task
  conflict требует нового preview, а не удаления по старому списку.
- [ ] Trusted agents: имя, endpoint, custom auth header и secret update; существующий
  secret не возвращается в UI. Доступны отдельные transient error состояния.
  Переписка с peer отображается в чате нашего агента без отдельного обхода policy.
- [ ] Cron: создание/изменение/выключение и ручной запуск через backend API,
  timezone и постоянный чат расписания. UI показывает skipped overlapping run;
  не пытается догнать пропуски собственным таймером.

## Проверки и завершение

Написана первая UI foundation: memory-only Keycloak adapter, owner identity,
общий paginated список чатов, Send/Get/Subscribe/Cancel, inline interactions,
settings/tool policy CAS и trusted-agent формы с write-only secret. Safe remote
progress выводится из server metadata без текста/IDs внешнего peer. Исходники UI
теперь читают bounded owner history API: несколько Task одного чата, уточнения,
queued input и owner review links выводятся одной лентой без дублирования final
result. Пагинация объединяет stable IDs; review cards остаются внутри своего
чата. Late GET/Send ACK и retry старой Task не заменяют уже наблюдённый новый root;
terminal state не регрессирует. Оба изменения прошли independent source review.

По указанию пользователя интерфейс следует структуре ChatGPT: светлый sidebar
с чатами и отдельными настройками, центральная лента, пользовательские сообщения
справа, нижний composer, Enter для отправки и Shift+Enter для новой строки.
HITL и вопросы владельцу отображаются inline. В заголовке чата добавлена панель
«Файлы»: read-only metadata, фильтры папки/возраста, server timestamp и bounded
pagination через реальный owner API. Abort и request generation исключают stale
ответы после смены context/filter/закрытия. Source helpers и форматирование
проверены; parent source review не нашёл блокеров. File picker, выбор и удаление,
attachment download появятся после готовности соответствующего backend flow;
cron остаётся открытым.
Наличие исходников и source review не отмечает browser acceptance выполненным.

Доступные реальные cached React/TypeScript/Vite packages позволяют запускать
typecheck; последний запуск завершается только TS2307 missing `keycloak-js`.
Package lock не придуман, замена official adapter не добавлена. Node проверка
actual `types.ts` подтверждает фильтрацию malformed/terminal progress metadata;
она не заменяет полный TypeScript/build/browser gate.

Текущая environment probe: Node/npm доступны, но `keycloak-js` отсутствует в
локальном npm cache, а registry request завершается `ENOTFOUND` configured proxy.
Повтор без proxy также завершается `ENOTFOUND registry.npmjs.org`.
Это препятствие установке/сборке в данной среде, не разрешение заменять OIDC
самописной схемой или считать browser gate пройденным.

- [ ] `npm ci`, typecheck и production build из `ui/` без незакоммиченного lock drift.
- [ ] Browser E2E с реальными ASGI/Keycloak/PostgreSQL: login/logout, shared chat,
  pending decisions, stale forms, upload/download/cleanup, cron и stream recovery.
- [ ] Keyboard/mobile layout, malicious message/file-name rendering, route access
  и отсутствие токенов в browser storage/network URLs/artifacts.
- [ ] Добавить browser gate в обычный CI и только после его прохода обновить
  implementation-status. Сохранить реальные blockers; build alone не доказывает
  авторизацию, chat durability или UI acceptance.
