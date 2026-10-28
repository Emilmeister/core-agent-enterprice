# Core Agent: завершение адаптации main для Cloud.ru AI Agents

> **For agentic workers:** Использовать `subagent-driven-development` для независимых проверок или `executing-plans` для последовательных этапов. Пользователь поручил обновить этот план и выполнить его до конца. Актуальный порядок и границы разрешения приведены в разделе от 28 октября; прежние разрешения в архиве не расширяют текущую задачу. Документ не утверждает готовность выпуска.

**Goal:** Довести `feat/cloudru-ai-agents` до рабочей поставки согласованного среза возможностей `main` на Cloud.ru AI Agents, сохранив утверждённые отличия платформенного профиля.

**Architecture:** Общие runtime, инструменты, UI, PostgreSQL и основной A2A 1.0 остаются едиными с main. Cloud.ru использует платформенный ingress, пароль владельца, совместимый внешний A2A 0.3, Landlock/seccomp и S3 для файлов; управление кластером не требуется.

**Tech Stack:** Python/uv, PostgreSQL, S3, Landlock/seccomp, официальный A2A SDK, React/TypeScript, существующие unittest и native/browser проверки.

## Актуальный план — 28 октября 2026

**Статус:** по отдельному поручению владельца после локальных проверок опубликован
linux/amd64 candidate `174dec5` с исправлениями UI; version tag и digest подтверждены
registry readback. Его развёртывание владелец выполнит позже; `latest` не обновлялся.
Предыдущий version candidate `948bae1` с исправлением неоднозначного GetTask HTTP 404
опубликован и проверен локально. После той публикации владелец сообщил об обновлении
образа в Cloud. Контролируемый live GetTask 404 → COMPLETED прошёл без повторной
отправки; фактический deployed tag/digest ещё не прочитан. `latest` при
нашей публикации оставался на `83edd16`. Ранее владелец подтвердил обновление
этого предыдущего образа с recovery, приватной диагностикой и output limit 16384.
На предыдущем образе прошли readiness/auth, обычный
ответ, dedup, полный ответ через SSE и создание/исправление приложения судоку
с публикацией файлов. Точный deployed digest независимо не прочитан;
подтверждение владельца и эти функциональные проверки не закрывают всю приёмку.
Контролируемый restart подтверждён операторскими логами: startup `11:31:33 UTC`,
application_ready `11:32:04 UTC`. Owner/HITL waits и два таймера сохранились.
Удалённое ожидание завершилось ошибкой после HTTP 404 на GetTask при restart,
хотя удалённая Task затем завершилась; active terminal D2 остаётся в
`WORKING` с `core_agent_execution.state=cleanup_pending`; replay не наблюдался, нет
доказательства остановки прежнего процесса. Эти два restart-пункта не пройдены.
Recovery-loop после non-retryable HTTP 404 исправлен локально в `83edd16`:
известный отказ сохраняется в `FAILED` до освобождения lease; повторный recovery
не вызывает provider. Подтверждены отмена, cooperative worker stop, incomplete
fallback budget finalizer и PostgreSQL reopen. В image прошли 23 regression
checks; совпали 138 source/UI hashes; registry readback подтвердил version tag.
Локальные native ARM64 Compose, restart, Kubernetes sandbox и Landlock gates
пройдены на том же коде candidate без source overlay. GitHub CI matrix,
native amd64 (включая обязательный browser job) и оставшиеся целевые Cloud
release proofs открыты. Дополнительный локальный ARM64 browser gate пройден
после исправления фокуса `d85744e` и затем повторно после исправления
перемонтирования карточек согласования; подробности ниже.

### Очередность: сначала локальные проверки

28 октября владелец уточнил порядок: сначала закрыть доступные локальные
проверки, затем возвращаться к Cloud.ru. Новые Cloud API-прогоны, обновления
и запросы перезапуска в этом локальном этапе не выполняются. Сравнительный
DOCX больше не обновляется по отдельному указанию владельца.
Позднее владелец отдельно поручил опубликовать готовый образ для самостоятельного
развёртывания. Это разрешение на публикацию version tag, без нового live-прогона
или изменения Cloud deployment со стороны агента.

Проверяется текущий код `948bae1`: recovery активной команды, ожидания,
полный цикл cron без HTTP, изоляция, PostgreSQL/S3 restore и настоящий browser
flow. Уже прошедшие проверки неизменного кода используются как evidence;
устраняются конкретные пробелы и прежние platform skips. Локальная среда —
ARM64; emulation не выдаётся за обязательный native amd64 gate.
Cloud-специфичными остаются фактическая замена экземпляра, ingress, idle CPU,
платформенные credentials и восстановление ресурсов самого развёртывания.

### Локальная проверка текущего кода — 28 октября

Runtime остаётся на `948bae1`: после него до начала этого этапа менялся только
план. Поэтому полный ordinary suite этого кода (2505 tests, exit 0, 16 skips)
и прежние неизменные native proofs используются повторно. Новые проверки
закрывают конкретные пропуски; skips не считаются пройденными gates.

| Проверка | Результат | Граница |
|---|---|---|
| Runtime-образ до исправления UI | Native linux/arm64, `core-agent-local-first-native:948bae1-arm64-20261028`, ID `sha256:b651e03b829e0eb5173a20a7fac0d0c1e47bee988c220d812a79aab2411f69f4`; совпали все 138 runtime/UI файлов | Только локальная сборка, не опубликована; no runtime overlay |
| Linux startup diagnostics | 4 прежних Linux-only теста прошли за 0,789 s, без skips; network none, dropped capabilities/read-only root, текущий образ | Проверка изоляции probes, ограничений вывода/времени и cleanup дочерних процессов; не proof enforcement Landlock |
| SGID и restore workspace | 3 прежних Linux-only теста прошли за 2,059 s, без skips; текущий образ, собственные PostgreSQL/MinIO | Сохранение режима каталогов и checkpoint/restore; portable test launcher не подменяет native sandbox gate |
| Полный automatic cron без HTTP | Реальный календарь и PostgreSQL coordinator приняли ровно одну Task, выполнили один model call и сохранили точный конечный ответ; после создания расписания HTTP отсутствовал | `ScriptedModel` и существующий classifier fixture, проверки включены, без policy changes/ручных решений; это локальный background proof, не проверка idle CPU Cloud |
| Recovery и checkpoint fencing | 37 существующих тестов прошли за 2,319 s, без skips, включая реальные PostgreSQL fences; отдельный Docker restart identity — 1 test за 2,232 s | 37 тестов используют текущий source; identity run использовал прежний ARM64 образ `83edd16` и mount текущего identity module, совпадающего с установленным. Не объявляется 38 проверками нового образа |
| Замена активного execution | Новый сценарий `test_replacement_waits_for_exact_stop_confirmation_without_replay` прошёл за 21,548 s, exit 0, без skips. A реально выполняет команду; при замене B прежний точный процесс ещё жив, B сохраняет cleanup_pending. Чужая generation отвергнута; после SIGKILL и Docker stopped-state точный CLI proof принят один раз, Task завершена `FAILED / SIDE_EFFECT_UNKNOWN`, replay отсутствует | Текущий packaged ARM64 runtime, только tests mount, реальные PostgreSQL и Bubblewrap/POSIX. HTTPS Origin и подписанная cookie проверяются; локальный HTTP представляет post-TLS hop, TLS этим не проверяется. Это общий recovery contract, не Cloud replacement/Landlock/S3 |
| Полный браузерный сценарий | Последний `CORE_AGENT_REQUIRE_BROWSER_TESTS=1`: 1 test, 197 проверок, 248,658 s, exit 0, без skips; реальные native ARM64 Pod, PostgreSQL, Keycloak и Mac Chromium. Пройдены файлы, согласования, история, peer panel, мобильная раскладка, logout и новый сценарий задержанного private material GET при изменении связи карточки с историей | Дополнительный ARM64 proof; не заменяет обязательный native amd64 CI. Контролируемое воспроизведение и границы вывода о прежнем сбое Inspect приведены ниже |

Приватные receipts/logs находятся в `.enterprise-work/` основного репозитория:
`local-first-startup-linux-20261028.{json,log}`,
`local-first-storage-scheduler-20261028.json`,
`local-first-recovery-20261028.json`,
`local-first-native-browser-20261028/result.json`,
`local-first-native-browser-20261028/focus-source-ui-hashes.json`.
Собственные storage/cron fixtures удалены; существующие пользовательские сервисы
не затронуты. Ошибки промежуточных cron fixtures сохранены отдельно от зелёного
результата; они не объявляются runtime-дефектами.

Единый native replacement-сценарий сохранён в `503433a`; prerequisites
добавлены в `AGENTS.md`. Повтор прежнего identity method на текущем образе
также прошёл: 1 test за 2,276 s, без skips; существующий mount identity module
сохранён. Ruff, 6 spec/lock checks и whitespace checks прошли. Frozen spec и
hash lock не менялись; пользователь разрешил изменения этих существующих tests.

Выявлен и закрыт пробел CI wiring: persisted replacement method требовал обе
PostgreSQL переменные, а прежний native restart step передавал только image,
поэтому новый method пропускался. Теперь этот же шаг в обоих native jobs
создаёт отдельный pinned PostgreSQL 17 в default Docker bridge с динамическим
loopback-портом, ограниченно ждёт readiness и передаёт `TEST_DATABASE_URL` /
`TEST_POSTGRES_CONTAINER`. EXIT trap удаляет только созданный container ID
и его anonymous volume; пользовательские сервисы не выбираются по имени.

Точный shell body этого CI-шага выполнен локально с итоговым ARM64 образом
`b50b6eb1`: оба restart/replacement tests прошли за 22,570 s, exit 0, без skips.
Docker readback подтвердил отсутствие нового PostgreSQL fixture; прежние
контейнеры сохранены. YAML parse, `bash -n`, actionlint 1.7.12 и whitespace
checks прошли; встроенные shellcheck/pyflakes не запускались из-за отсутствия
этих отдельных инструментов. Receipt: `local-first-restart-ci-20261028/result.json`.
Это локальное исполнение CI-команд, а не результат GitHub matrix: native amd64
и особенности Ubuntu hosts ещё должны пройти обязательный CI gate.

Браузерный прогон нашёл дефект `Chat.closePeer`: подключённая к DOM кнопка
открытия внутри закрытого `details` не может получить фокус. В `d85744e`
используется уже существующая видимая кнопка панели как fallback; свёрнутая
группа не раскрывается. Точная прежняя проверка Escape сохранена и прошла.
Также исправлены три предпосылки browser fixture: выбирать только Open/Download
при проверке busy, открывать сохранённый чат после reload и дождаться сообщений
после мобильного remount. Outcome, security и layout assertions не ослаблялись.
Нормативный UI-контракт не менялся; стабильные сведения `AGENTS.md` актуальны.

На Node 24.8.0 прошли `npm ci`, `npm test`, `npm run typecheck`, `npm run build`;
lockfile не изменился. Для браузера собран отдельный локальный образ
`core-agent-local-first-native:peer-focus-arm64-20261028`, ID
`sha256:b79effc6b8846f0c3262b46764800019429c4857d50456dd3285d4e183e7fe0f`.
Это derived image от `b651e03`, с удалением прежнего `ui_dist` и полной заменой
пересобранным UI; не полный rebuild исходного Dockerfile. Полный набор и bytes
138 packaged Python/policy/UI файлов совпали, старые assets отсутствуют.
Runtime overlay не применялся. Образы сохранены локально, не опубликованы;
собственные browser cluster/services/network удалены.

Отрицательные результаты browser attempts 1–5 сохранены. Для прежнего Inspect
failure точное чередование событий не записано. Последующий контролируемый
прогон настоящих `History`/`InteractionCard` воспроизвёл тот же симптом:
при появлении или исчезновении связи review с историей карточка с тем же wait ID
перемонтировалась, отменяя private GET и сбрасывая состояние. Загрузка более
ранней записи того же root также меняла ключ section и сбрасывала карточку.
Это доказательство дефекта и механизма, а не ретроспективное доказательство
точного чередования событий в историческом attempt 5.

Карточки теперь имеют один уровень React siblings и стабильные ключи wait ID;
section использует root и номер его сегмента, сохраняя различимость A/B/A
при локальном echo. Anchor pending approval перенесён на существующий article.
Состояние private material и ответа остаётся внутри `InteractionCard`, без кэша
или переноса authority. Контролируемые проверки подтвердили сохранение GET,
материала, черновика и фокуса при связывании/отвязывании и старой pagination;
закрытие wait очищает материал, настоящий unmount отменяет запрос.

В существующий разрешённый browser test добавлена задержка настоящего material
response и временное удаление только ссылки этого wait из настоящего history
response с последующим восстановлением. Проверяются тот же DOM-узел, фокус,
отсутствие отмены запроса и отображение исходного материала после release.
Первый полный прогон новой версии выявил пустую обёртку после закрытия карточки;
обёртка удалена в коде, исходная проверка доступности итогового ответа сохранена.
Второй полный прогон прошёл за 248,658 s. Прежние outcome/security/layout
assertions не ослаблялись; нормативный контракт и frozen spec не менялись.

На Node 24.8.0 прошли `npm ci`, `npm test`, `npm run typecheck`, `npm run build`;
после удаления обёртки test/typecheck/build повторены, lockfile не менялся.
Итоговый локальный образ `core-agent-local-first-native:guardrail-direct-arm64-20261028`,
ID `sha256:b50b6eb1fdd814d5d1477df1ac1a7ffad1c542544a91a210b55f943a5196f344`,
собран от `b651e03` полной заменой `ui_dist`. Совпали набор и bytes всех 138
packaged Python/policy/UI файлов; source inputs не менялись при сборке.
Это derived image без runtime overlay, не полный rebuild исходного Dockerfile;
образ не опубликован. Evidence: `local-first-native-browser-20261028/guardrail-final/`,
`guardrail-controlled-reproduction.json`, `guardrail-controlled-green-direct.json`
и `owner-question-pagination-green-direct.json` в родительском evidence-каталоге.
Собственные browser cluster/services/network и диагностические Storybook/Chrome
остановлены и удалены; существующие пользовательские сервисы не затронуты.

До объявления полной миграции завершённой остаются native amd64 CI
и перечисленные ниже платформенные proofs.
Локальные проверки не подтверждают фактическую остановку старого Cloud execution,
deployed digest, ingress, idle CPU, production credentials или Cloud backup/restore.

### Границы текущей работы

- Рабочая копия: постоянный `.enterprise-work/cloudru-ai-agents`, ветка
  `feat/cloudru-ai-agents`. Существующие реализации и receipts используются повторно.
- Из SGR перенесён только `core_next_steps`, включая policy/HITL, cadence и owner UI
  (`410a646`, `55ab58c`). Пункты 2/3 SGR не перенесены и не включаются автоматически.
  На исходном `18cafd6` сравнение `main...HEAD` даёт `5 50`; старое утверждение
  «весь текущий main входит в Cloud» больше не описывает состояние веток.
- Telemetry исключена пользователем из этого этапа. Существующие доказательства
  доставки OTLP не дополняются догадками. Native события compaction нужны только
  как доказательство соответствующего runtime-сценария из доступных журналов.
- Изменения frozen tests/spec ограничены согласованными recovery/diagnostics
  исправлениями и отдельно разрешёнными локальными browser/replacement тестами;
  этот план не разрешает перенос остальных SGR-контрактов или
  изменение любых других замороженных требований ради зелёной проверки.
- Оператор управляет Cloud deployment через UI платформы. План не требует от него
  `kubectl`, shell в Pod, прямого SQL или доступа к серверу БД. Проверки приложения
  выполняются через существующие owner UI/API и ограниченные agent tools; отдельные
  native CI fixtures относятся к среде разработки, а не к управлению Cloud-кластером.

### Что уже завершено и чем подтверждено

| Слой | Подтверждённый результат | Граница доказательства |
|---|---|---|
| Код и конфигурация | Persistent memory gate, production Cloud/PostgreSQL/S3/Landlock config, required native CI wiring (`cbfca2d`); S3 cancellation fencing, cold file projections, peer discovery/polling, compaction framing и pinned kernel recovery (`619f157`) сохранены в ветке | Наличие кода и CI gate не подтверждает выполнение gate на целевой платформе |
| Последующие изменения | Полный финальный ответ без response-control tool (`547cb98`), optional `core_next_steps` и UI, output default 16384 (`a481655`), private error causes (`18cafd6`) | Новые изменения после batch4 требуют своих proofs; прежние Cloud receipts относятся к своим версиям |
| Ordinary suite пакета `18cafd6` | 2498 tests за 432,598 s, exit 0, 16 skips; `error-logs-full.log` | Локальные fixtures; skips не закрывают обязательные native/browser проверки |
| Recovery fix 28 октября | `83edd16`: 293 targeted tests; 2503 tests полного ordinary suite за 715,572 s, exit 0, 16 skips; Ruff и 6 spec/lock checks прошли. Независимый review нашёл два дополнительных edge case, оба исправлены и покрыты проверками | Все новые regression proofs входят в ordinary suite; native/browser и Cloud rollout нового пакета остаются отдельными gates |
| Image `18cafd6` | linux/amd64; 17 diagnostic tests без skips; совпали 138 source/UI hashes, default 16384 проверен в image; version tag и `latest` прочитаны из registry | Проверка прежнего опубликованного пакета, а не digest работающего Cloud deployment |
| Новый image `83edd16` | linux/amd64, user `agent`, revision label проверен; 23 diagnostic/recovery tests; совпали 138 source/UI hashes; изменён только `core_agent/runtime.py` поверх pinned `18cafd6`; immutable version tag прочитан из registry | Обновление Cloud подтверждено владельцем; точный deployed digest, native amd64/browser и оставшиеся platform gates ещё не подтверждены |
| Cloud functional proofs | A2A 0.3/1.0, owner login/history/download, оригиналы и immutable results, MCP discovery/call, memory/NER, skills, terminal/Python, root/child workspace, background cancel и remote Send/Get/Cancel | Подробные сценарии и ограничения сохранены в архиве; полной матрицы на последнем image ещё нет |
| Cloud readback 28 октября | Health/live и ready, owner login, settings revision 22 и FM model discovery вернули HTTP 200; выбранная `Qwen/Qwen3.5-397B-A17B` есть в каталоге; context 128000, structured reasoning off/chat_template, next steps enabled/every=5 | Только чтение настроек и доступность API; не новый model/tool run и не сверка deployed digest. Receipt: `cloudru-current-readiness-20261028.json` |
| После обновления Cloud 28 октября | Health, owner auth и доступная выбранная Qwen подтверждены; неавторизованный owner GET и wrong-Origin logout отклонены, рабочая сессия сохранилась. Ответ `323` на 17×19 завершён за 2 хода модели с `core_next_steps`; duplicate Send вернул прежнюю Task без расходования budget, изменённый Message с тем же ID дал `MESSAGE_ID_CONFLICT` | Receipts `cloudru-post-rollout-readiness-20261028.json`, `cloudru-post-rollout-simple-20261028.json`; session expiry/rotation и external isolation этим не закрываются |
| Полный ответ через SSE после обновления | Follow-up в том же чате создал новую Task; stream содержал SUBMITTED, WORKING без текста и COMPLETED с единственным полным ответом 1…30. Ровно 3 события, итог точный, complete=true, 2 хода модели, logout 200 | Receipt `cloudru-post-rollout-stream-20261028.json`; проверены фактические события одного нового stream, не произвольное сетевое разъединение |
| External isolation после обновления | Собственные owner Task/history/file доступны; canonical и `/a2a/external/` Task GET возвращают A2A `TASK_NOT_FOUND` и с owner cookie, и без неё. Owner history/download без сессии дают `401 AUTHENTICATION_REQUIRED`. Исходный Sudoku snapshot совпал по SHA | 10 checks, receipt `cloudru-post-rollout-external-isolation-20261028.json`. Это разделение owner/external; разных внешних callers профиль Cloud не различает — у них общий platform principal |
| Судоку после обновления | Исходная задача завершена за 4/10 ходов, 3 tools; файл опубликован. Независимая проверка обнаружила пропущенный `fill_box`. Агент исправил свой код follow-up за 8/10 ходов, 7 tools; syntax, генерация/решение easy/medium/hard, строки/столбцы/блоки и clues прошли независимо. Старое вложение осталось побайтно неизменным | Ошибка была в сгенерированном приложении; прежний runtime budget/recovery сбой не повторился в обоих запусках. GUI не запускался. Receipts `cloudru-post-rollout-sudoku-20261028.json` и `cloudru-post-rollout-sudoku-repair-20261028.json`; глобальные settings и guardrails не менялись |
| Cloud restart proofs | Owner/HITL waits и memory пережили rollout; peer context сохранился между двумя операциями, файлы совпали по bytes/digest | Timer и background command завершились до прежнего rollout: активное execution через замену ими не проверено |
| Локальные durability/security proofs | 174 storage/recovery проверки, ещё 56 security/config/restart проверок; coordinated PostgreSQL/S3 restore и runtime role grants | ARM64 VM/source overlay, инъекция S3 errors и disposable restore не заменяют native matrix, Cloud outage или target restore |
| Дополнительные image gates 28 октября | Exact `18cafd6` Docker restart identity: 1 test passed. Дополнительная amd64 QEMU VM: 34 tests, zero skips, одна ошибка preflight после 10,027 s; cleanup подтверждён, OOM отсутствует | Native gate на ARM64 Docker отказал из-за несовпадения архитектуры; QEMU-прогон не прошёл и не заменяет native proof. Browser fixture пока отсутствует. Receipt: `cloudru-remaining-local-gates-20261028.json` |
| Native ARM64 Compose и restart 28 октября | Кандидат из исходного Dockerfile, linux/arm64, user `agent`, revision `83edd16`; все 138 source/UI hashes совпали с опубликованным пакетом. Существующий CI composition-root test terminal/Python/background прошёл за 23,601 s; restart identity — 1 test за 2,095 s. Exit 0, без skips и source overlay | Локальные ARM64 Compose/Bubblewrap и restart subsets. Image не опубликован; собственные containers/volumes/network удалены. Receipt и log: `cloudru-recovery-arm64-compose-20261028.{json,log}` |
| Native ARM64 Kubernetes sandbox 28 октября | 15 tests за 160,095 s, без skips; Pod Succeeded/exit 0, `hostUsers: false`, node readback `pids.max=512`. Сохранены исходные node digest, dual-stack fixture, OCI profile, ресурсы и test command; установлен точный проверенный image | Адаптация fixture ограничена Darwin CLI той же версии с официальными checksums и уникальным image reference. Собственные cluster/receiver/network удалены. Receipt и log: `cloudru-recovery-arm64-kind-20261028.{json,log}`; это локальный Pod, не Cloud deployment |
| Native ARM64 Landlock 28 октября | Неизменный `deploy/landlock-native.sh`: 40 tests за 9,004 s, exit 0, zero skips. Изолированная Apple VZ VM, aarch64 Linux 7.0.0-34, Docker 29.9.2, Landlock ABI 8; точный image ID и test/profile archive SHA совпали. Только tests mount, исходный Moby profile, без runtime overlay | Аппаратная виртуализация ARM64, не QEMU TCG. Локальная guest proxy ошибка установки Docker устранена до gate без изменения host proxy. Собственные VM/state/transfer archives удалены; Lima 2.2.1 осталась установлена. Receipt и log: `cloudru-recovery-arm64-landlock-20261028.{json,log}`. Kernel и outer profile целевого Cloud этим не проверены |
| Restart: owner/HITL и память | Сохранились прежняя owner cookie, настройки, точные owner-question/HITL wait IDs и содержимое revision-1 canary. B завершилась полностью, проверила read и один CAS delete | A сохранила и прочитала note, но завершилась `complete=false / budget_exhausted` после невалидных/недоступных planning calls; вся A/B recipe не объявляется пройденной. Receipt: `cloudru-rollout-ab-final-readback-20261028.json` |
| Restart: таймеры | C и R сохранили единственный исходный `core_wait_until`; deadline 11:38:01 UTC, woke_at 11:38:02.083576 и 11:38:01.520273 соответственно; обе Task COMPLETED | Это восстановление таймеров, не доказательство remote GetTask continuity. Receipt: `cloudru-rollout-final-readback-20261028.json` |
| Restart: удалённое ожидание — локальное исправление, Cloud gate открыт | Одна отправка, без replay; operation `26475d52-7279-5bb1-8ed3-362e1f02e9bb` получила GetTask HTTP 404 в 11:31:50 UTC и стала failed с unknown remote outcome. Удалённая R позднее завершилась. В 948bae1 неоднозначный 404 повторяется в рамках исходного poll interval/deadline; typed A2A error остаётся окончательным | Происхождение реального 404 от ingress — гипотеза: body не сохранён. Локальный ordinary suite 2505 tests, image 82 tests прошли; version image опубликован. После сообщения об обновлении пройден отдельный live 404 → COMPLETED; реальное remote-wait через restart требует повторного evidence. Receipt: `cloudru-rollout-operator-log-facts-20261028.json` и `cloudru-remote404-image-20261028-948bae1.json` |
| Restart: active terminal D2 — незакрытый gate | START в 11:29:39.606495 UTC, ready нового экземпляра в 11:32:04 UTC до планового конца 11:38:39.606495; один исходный terminal call, replay не наблюдался. Поздний Task GET показывает `cleanup_pending` | Остановка старого процесса не доказана. Отсутствие START/DONE после замены не является доказательством остановки или потери уже committed checkpoint. Receipt: `cloudru-rollout-d2-late-readback-20261028.json` |
| JSON control после restart | Новый запрос с точным JSON-примером вернул весь требуемый объект без fences, сохранил `PENDING; NOT RELEASED`; complete=true, 2 model turns | Короткий control не доказывает compaction. Receipt: `cloudru-post-restart-json-control-20261028.json` |
| Контролируемый GetTask 404 после сообщённого обновления — пройдено | Task `6f1de1ae-15a3-4e0e-a609-a78db64e1bae`, operation `45eb67cb-ea9a-552f-8ce8-f410c27e28a0`: один Send, Get plain HTTP 404, следующий Get той же remote Task — COMPLETED. Интервалы 11,649 s и 10,658 s; operation completed, outcome_unknown=false. Ровно один успешный send и wait; parent COMPLETED/complete=true с точным контрольным marker | Первое окно 180 s закончилось до разрешения собственного wait; последующее точное HITL approval завершило тот же root без новой отправки. Peer удалён по исходной revision 1, отсутствие подтверждено, logout 200; fixture/tunnel остановлены, временный ключ удалён. Это проверка поведения на текущем Cloud, не actual restart, сверка image digest или наблюдение private deadline. Receipt: `cloudru-remote404-live-proof-20261028.json` |
| Проверка после сообщённого обновления образа | В 13:00:56–13:01:31 UTC live/ready 200, owner API 401 без session и успешный login. Обе compaction Task сохранили прежние конечные состояния и все 14 history items каждая. Новая Task `e1362104-4cab-4339-bb4b-140b29da9181` вернула точно 323, complete=true, 2/10 model turns. Logout 200, settings не менялись | D2 по-прежнему WORKING; только чтение, без cancel/replay/stop attestation. Это проверка доступности и сохранности после сообщения владельца, не независимый proof deployed digest или GetTask 404 recovery. Receipt: `cloudru-post-user-rollout-verification-20261028.json` |
| Planning OFF/ON после restart | OFF: Python→Python, ноль planning calls. ON/every2: next_steps→Python→Python→next_steps. Обе Task completed/complete=true, точный ответ72, failed calls0. Исходный next_steps=null восстановлен сразу после admission и до initial-material allow, revisions24/26 | Проверены pinned settings обеих задач и cadence; два compactions ещё отдельный gate. Receipt: `cloudru-next-steps-live-20261028.json` |
| Две pressure-порции после restart — не пройдено | Task `709d2ff2-8c0a-4938-abd5-223930d7842e` приняла две порции по 14100 bytes и ответы на оба owner question. Native logs подтверждают: после первой порции сжатия нет; после второй две попытки, turns 6/7, обе `summary_too_large`, затем `FAILED / CONTEXT_UNRECOVERABLE` в 12:21:56 UTC. Успешных `context.compacted` нет | Реальная working_capacity 8016: порог 7214,4, pinned 900, лимит summary 302 и 241 при повторе. Исходный расчёт probe был неточен: первая порция не создала pressure. Timeout/provider failure отсутствует. context_window 128000 восстановлено при admission, revision 28; logout 200. Receipt: `cloudru-two-compactions-live-20261028.json`; native events — предоставленные оператором логи |
| Исправленный pressure probe — два сжатия подтверждены, строгий ответ не прошёл | Task `a97f12e0-6038-4c52-bdac-92970e70275b`, run `5f0acff8-b861-4567-8acb-f8efaddc9a43`: native `context.compacted` в 12:45:02 UTC — 8402→853, диапазон [1,6]; в 12:46:48 UTC — 8977→902, диапазон [1,11]. W=8016, итог 10,64% и 11,25%; обе первые попытки, ошибок сжатия в предоставленном интервале нет. COMPLETED/complete=true, 8/10 model turns, оба question/call IDs сохранены без повторов | Final содержит fences и batch_1/batch_2/final вместо одного требуемого объекта; release=PENDING вместо точного `PENDING; NOT RELEASED`. Route/artifact/amount верны. Это отрицательный результат конкретного строгого output check, не установленный дефект миграции или потери данных при сжатии: summary content не доступен, а исходное значение содержит некавыченный delimiter. Settings восстановлены при admission, revision 30; logout 200. Receipts: `cloudru-two-compactions-valid-live-20261028.json`, `cloudru-two-compactions-valid-native-events-20261028.json` |



| Cloud idle cron 28 октября | Один automatic root создан в 09:07:01.599 UTC, через 1,599 s после due и до первого reconnect в 09:08:30 UTC. Наша тишина — 11m57s; владелец подтвердил закрытый UI к 09:00:38 UTC. Постоянно включённый 1 экземпляр подтверждён владельцем | Initial-input classifier за 0,507 s дал `suspicious`, поэтому unattended completion не доказан; timeout/provider error отсутствовал. Собственное расписание удалено, Task отменена. Receipt: `cloudru-cron-idle-20261028.json`; фактический HTTP-трафик платформы независимо не захватывался |

После локального этапа по явному поручению владельца опубликован новый candidate:

```text
blockchain.cr.cloud.ru/core-agent-gn:cloudru-local-verified-20261028-174dec5
sha256:99d59d07bc94ce4d607cc8e607962f9c801d0cc3c7e00f16349c39f4bd568e69
```

Registry readback подтвердил linux/amd64, revision `174dec5`, user `agent`,
совпадение manifest digest и слоёв с локальным candidate. База — pinned image
`948bae1`; полностью заменён `ui_dist` из проверенной браузером сборки.
Совпали все 138 packaged files: 74 неизменных runtime-файла и 64 UI assets;
29 UI source/build inputs также совпали с browser receipt. Базовые слои сохранены,
прежних UI assets в итоговом inventory нет. Import smoke `app/a2a_sdk/runtime/ui`
прошёл внутри amd64 image без сети, source overlay и root-запуска.
Это derived image, не полный rebuild Dockerfile; amd64 smoke на ARM64 Docker
использует emulation и не закрывает native amd64 gate. `latest` оставлен прежним,
Cloud deployment не изменялся. Receipt и build/push/readback logs:
`cloudru-local-verified-image-20261028-174dec5/published.json` и соседние файлы
в приватном `.enterprise-work/` основного репозитория.

Предыдущий candidate опубликован отдельным version tag. Владелец сообщил об обновлении
образа в Cloud; точная revision пока не подтверждена независимо:

```text
blockchain.cr.cloud.ru/core-agent-gn:cloudru-remote404-20261028-948bae1
sha256:eb627c0d83144c965a0e2726434469ff44aedef93f68c70f71ebf9558fdc18d1
```

Registry подтверждает linux/amd64 и revision 948bae1. Все 138 runtime/UI файлов
совпали с ожидаемым inventory; изменён только remote_agents.py относительно
предыдущего image. Внутри образа 82 remote transport/operation tests прошли
за 1,604 s, exit 0, без skips, source overlay отсутствует. Запуск на ARM64
Docker Desktop с amd64 emulation не заменяет native amd64/browser gates.
Receipt: `cloudru-remote404-image-20261028-948bae1.json`.

`latest` остался на предыдущем image; именно его обновление в Cloud ранее
подтвердил оператор:

```text
blockchain.cr.cloud.ru/core-agent-gn:cloudru-recovery-20261028-83edd16
sha256:cdc92975968525ef2d6b9a31cfcca31c0bc526f2012e41db73d58bd2043e3746
```

Новые receipts: `cloudru-recovery-image-20261028.json`,
`cloudru-recovery-image-smoke-20261028.json`,
`cloudru-recovery-image-tests-20261028.log`,
`cloudru-recovery-version-registry-20261028.txt`.
Version tag и `latest` прочитаны из registry с одинаковым указанным digest.
После публикации владелец сообщил об обновлении Cloud через UI платформы;
независимая сверка digest запущенного экземпляра остаётся открытой.

Отдельно от исправления fixture выявлен разрыв нормативного summary budget:
при W=8016 и pinned=1201 остаётся 1 токен, при pinned=1202 — уже 1202.
Это следствие текущего правила `spec/context.md`, а не расхождение кода со spec.
Предложение ожидает нового разрешения на frozen spec/tests: считать summary
и сохраняемый unpinned остаток как 10–15% от W−pinned, при необходимости
отпускать optional overlap до выделения summary, сохраняя pinned дословно.
Повтор остаётся 80% от первого budget, максимум 2 оплачиваемые попытки;
общий контекст не превышает W, но может превысить 15% W из-за pinned.
Для pinned 900 это 1067 и 853 вместо 302 и 241. До согласования расчёт не меняется.
Также numeric working_tokens ошибочно скрывается общим redaction по имени
ключа; предложено добавить только валидированный числовой counter в существующий
runtime log allowlist, не ослабляя фильтрацию секретов.
Диагноз: `cloudru-two-compactions-native-diagnosis-20261028.json`.

Публикация предыдущего `18cafd6` подтверждена 27 октября в `19:00:23 UTC`.
Receipts находятся в
ignored `.enterprise-work/` основного репозитория, рядом с Cloud worktree:
`cloudru-error-diagnostics-image-20261027-18cafd6.json`,
`cloudru-error-diagnostics-image-smoke-20261027-18cafd6.json`,
`cloudru-error-diagnostics-image-tests-20261027-18cafd6.log`,
`cloudru-error-diagnostics-version-registry-20261027-18cafd6.txt` и
`cloudru-error-diagnostics-latest-registry-20261027-18cafd6.txt`.

- [x] Сохранить реализованные Cloud configuration/memory/native CI изменения и их локальные proofs (`cbfca2d`).
- [x] Перенести согласованный `core_next_steps`, UI-настройку и проверить ordinary suite соответствующего пакета.
- [x] Проверить и опубликовать пакет `18cafd6` с output default 16384 и приватной диагностикой.
- [x] Сохранить прежние Cloud receipts с их фактическими ограничениями; не переносить результаты между версиями автоматически.

### Оставшийся порядок выполнения

#### 1. Завершить recovery fix и локальную регрессию

Файлы: общие model/recovery paths в `core_agent/runtime.py`; существующие
`tests/test_runtime_observability.py`, `tests/test_postgres_persistence.py`,
`tests/test_interactions.py`. Связанное требование RUN-05 и точный spec lock
обновляются только в пределах разрешённого исправления.

- [x] Воспроизвести non-retryable model failure через recovery, включая повторное открытие PostgreSQL state; проверить число model calls и конечное состояние Task.
- [x] Исправить общую точку завершения: окончательный provider failure сохраняется и освобождает чат; следующие recovery scans не запускают тот же model call. Сохранить действующие retryable budgets, cancellation, lease fencing и запрет повторного исполнения tools. Cooperative shutdown не теряет уже полученную ошибку; malformed budget finalizer сохраняет incomplete fallback.
- [x] Выполнить targeted proof, затем Ruff, полный ordinary suite и spec checks на итоговых исходниках. Проверенное изменение сохранено в `83edd16`.

Логи: `cloudru-recovery-targeted-final-20261028.log` (293),
`cloudru-recovery-full-final-20261028.log` (2503, 16 skips),
`cloudru-recovery-ruff-all-20261028.log`, `cloudru-recovery-spec-final-20261028.log`.
Предыдущий полный прогон был прерван ради исправления найденных review edge cases;
он не используется как доказательство готовности.

Команды из Cloud worktree с существующими локальными disposable fixtures:

```sh
uv run --env-file ../cloudru-next-steps-fixtures-20261027.env python -m unittest tests.test_runtime_observability tests.test_postgres_persistence tests.test_interactions -v
uv run ruff check core_agent tests
uv run --env-file ../cloudru-next-steps-fixtures-20261027.env python -m unittest discover -s tests -v
uv run python -m unittest tests.test_spec_quality tests.test_spec_lock -v
git diff --check
```

Fixture ENV остаётся локальным, credentials не копируются в план или журнал.
При недоступности fixture сначала восстановить собственную тестовую среду;
skip обязательного PostgreSQL proof не является успешной проверкой.

#### 2. Проверить новый пакет и обязательные native/browser gates

- [x] Устранить ошибку workflow, мешавшую запуску jobs: job-level `env` использовал недопустимый контекст `runner.temp`; путь `KUBECONFIG` теперь задаётся первым runner step через `GITHUB_ENV` (`873de0d`). Checksum-verified actionlint 1.7.12 воспроизвёл ошибку до изменения и прошёл после. Проверены выражения/YAML и shell-вывод пути с пробелом; embedded shell/python linters и сами native jobs этим не заменяются.
- [x] После исправления собрать один candidate `83edd16`, сверить runtime/UI bytes, выполнить image checks и сохранить immutable tag/digest. Образ `18cafd6` не содержит recovery fix; новый candidate опубликован отдельным version tag.
- [x] Дополнительно выполнить неизменный CI Compose command на native ARM64 Docker с локальной сборкой исходного Dockerfile и установленным кодом `83edd16`: composition-root terminal/Python/background test прошёл без skips. Локальный ARM64 image `sha256:58755bc69f28eb2619077a0f8d9a18da47bc23c453a78ac4f7d4114b33bf749e`; only-test mount, код образа не подменялся, все 138 source/UI hashes совпали. Это отдельный subset, не закрытие всей matrix.
- [x] На том же ARM64 image выполнить restart identity (1 test), полный required sandbox Pod gate (15 tests, `pids.max=512`) и исходный required Landlock gate (40 tests) в аппаратной VZ Linux VM. Все exit 0, без skips; provenance и cleanup записаны в receipts выше. Эти локальные результаты не объявляются GitHub CI run или native amd64/Cloud proof.
- [ ] Выполнить существующую CI matrix `sandbox-linux` на native amd64/arm64 с candidate image: Landlock/seccomp, restart identity, Compose Bubblewrap и sandbox gates; на amd64 — dedicated browser/Keycloak/PostgreSQL gate. Сохранить результаты каждой архитектуры без замены required flags на skips.
- [x] Для UI fix `d85744e` выполнены из `ui/` `npm ci`, `npm test`, `npm run typecheck`, `npm run build` с Node.js 24.8.0, lock неизменён. Dedicated browser сценарий дополнительно прошёл на локальном ARM64 image: 194 checks, без skips; это не amd64 CI. Packaging contract не менялся; точные image/UI bytes сверены. Одиночный предыдущий Inspect failure сохранён как открытая диагностика.

Команды существующих gates на подготовленных fixtures из `.github/workflows/ci.yml`:

```sh
LANDLOCK_TEST_IMAGE=core-agent:sandbox bash deploy/landlock-native.sh
CORE_AGENT_RESTART_TEST_IMAGE=core-agent:sandbox uv run python -m unittest tests.test_execution_restart_docker -v
CORE_AGENT_REQUIRE_BROWSER_TESTS=1 uv run python -m unittest tests.test_owner_ui_files_browser -v
```

Первая команда сама включает required Landlock/seccomp flags. Browser-команда
использует disposable Pod, Chromium, PostgreSQL и Keycloak, подготовленные CI.
Docker LinuxKit без Landlock уже отклонил required gate; повторение той же среды
не заменяет native Linux runner. Эти prerequisites обеспечиваются в CI/локальной
среде разработчика без запроса доступа к Kubernetes целевого Cloud deployment.

28 октября проверен удалённый CI: последние запуски завершались до создания jobs;
последний исторический успешный запуск содержал только `verify`. Свежего native
proof нет. В этой сессии отсутствует авторизованный GitHub transport: HTTPS
credentials не найдены, SSH с проверенными официальными host keys отклонил
предложенные ключи. После сохранения исправлений нужен авторизованный push этой
ветки; существующий push-trigger запускает `verify`, затем обе native jobs и
amd64 browser gate. Это доступ к репозиторию CI, не к Cloud Kubernetes/БД.

#### 3. Закрыть функциональную матрицу на согласованном candidate

Через owner UI/API использовать только собственные чаты, записи и временные
подключения; сохранить исходные settings и восстановить их после проверки.

| Проверка | Уже есть | Что ещё должно быть в итоговом receipt |
|---|---|---|
| Модель, контекст, next steps | После обновления: обычный ответ, SSE без частичного текста, Python/file tools, repair follow-up; live planning OFF/ON every2, cadence и snapshot; native logs подтвердили два compactions с итогом 10,64% и 11,25% W, без повторных owner questions | Provider failure становится terminal в Cloud; строгий final format и точное сохранение всех полей пока не прошли; содержание summary отдельно не наблюдалось |
| Формат ответа | После restart точный JSON-пример и literal `PENDING; NOT RELEASED` прошли; прежние длинные controls дали fences/`PENDING` | Сохранить факты через два подтверждённых compactions; отрицательные старые exact checks не приписывать compaction без evidence |
| Memory/skills | CRUD/read после restart, NER, child allowlist, activation/resources; прежние search proofs | Исправить/проверить соответствие embedding dimension 2048 и configured index 1024; startup vector_index=true и отсутствие degraded_channels не доказывают подходящий индекс. Применимые permission negatives остаются |
| MCP | Публичный Microsoft Learn discovery/call и локальные policy/storage tests | Контролируемый HTTPS peer с credentials, deny/HITL и reconnect после rollout |
| A2A и файлы | Прежние 0.3/1.0 и remote proofs; после обновления — dedup/conflict, terminal SSE, новый follow-up, download/raw A2A bytes и immutable старое вложение после исправления | Применимые reconnect/push и холодные mixed/large artifacts на новом candidate; push — только если включён |
| Owner UI и доступ | После обновления — login/logout/history/download, unauthenticated owner denial, wrong-Origin denial без потери сессии, owner/external Task isolation | Итоговые settings/peer/material cards, session expiry/rotation; общий external principal остаётся утверждённым ограничением Cloud-профиля |
| Local agents/terminal | Shared root/child workspace, Python broker, timeout/cancel и отдельный cross-chat denial | Два одновременно работающих чата, root/child cancellation/checkpoint и cleanup при замене экземпляра |

- [x] Оператор сообщил об обновлении image; readiness, owner login и успешные model/tool runs после обновления подтверждены. Точный deployed digest остаётся явно непроверенным: Agent Card version `1.0.0` не идентифицирует сборку.
- [ ] Для каждой строки сохранить результат «прошло / воспроизводимый дефект / ограничение профиля» с Task IDs, версией candidate и безопасными receipts; исправлять только выявленные дефекты в общей точке.

Task IDs нового функционального прогона: обычный ответ
`fcaf582d-ce96-472e-abae-44551b7c990d`, streaming follow-up
`d9861256-9aa9-4c80-b90e-abea36fa6caa`, судоку
`70cd023c-512d-4d18-8e5c-6ded8e4fb35c`, исправление судоку
`09913c8c-163e-4b2d-bfd2-89e4c7ae6635`. Исходный файл: 10387 bytes,
SHA-256 `881b37a6cf48447e95b161f245de2df231c0a305434371054d8564a02cfa72b2`;
исправленный: 10744 bytes,
SHA-256 `6e9b994b96ec2a1cbf6d11a20529fb47fa8b7af12afb2840aa2e4e0b3dcdcc6f`.
Оба опубликованных snapshot прочитаны после исправления и проверены независимо.

#### 4. Получить оставшиеся platform proofs через UI оператора

Эти пункты требуют действий или evidence оператора платформы. Агент заранее
готовит ограниченный сценарий и собственные тестовые данные; оператор выполняет
доступное действие в Cloud UI. Недоступная функция UI остаётся конкретным
platform blocker, её нельзя считать выполненной или обходить прямым доступом к БД.

Проверка официальной документации 28 октября уточнила границу: отдельный
Container Jobs имеет UI запуска команд, но доступ из него к инжектируемой
AI Agents БД не подтверждён; FAQ Container Apps исключает подключение к Managed
PostgreSQL, а тип нашего DB resource не установлен. Поэтому создание job не
считается готовым способом выполнения migration. Для production нужны
подтверждённые execution environment/network/DDL credential/runtime-role workflow
и согласованное восстановление фактической БД с referenced S3 objects.
Источники и критерии для migration, grants, restore, secrets и overlap собраны в
[операторской инструкции](../../cloudru-trial.md#доступ-только-через-ui-платформы).
Никакие новые Cloud ресурсы при этой проверке не создавались.

- [ ] Проверить длительный idle и фактическое выделение CPU без HTTP. 28 октября владелец подтвердил для нашего deployment: «всегда 1 включен». Это подтверждение настройки владельцем, не независимое API readback. В [официальной инструкции создания](https://cloud.ru/docs/ai-agents/ug/topics/guides__create-agent) есть опция «Не выключать агента»; [описание масштабирования](https://cloud.ru/docs/ai-agents/ug/topics/concepts__scaling) связывает постоянную работу с минимумом экземпляров больше нуля. Cron, background и durable waits должны завершиться без запроса, запускающего работу. Прежние 3/5 минут тишины не подтверждают длительный idle или непрерывное выделение CPU.

  Прогон 28 октября подтвердил автоматический старт cron внутри окна 11m57s
  без наших запросов, до первого reconnect. Task `e4048e24-52de-49e2-81e7-832978ca75ca` ожидала решения
  по материалу: `suspicious`, 1 call, 215 prompt / 9 completion tokens, 0,507 s.
  Это настоящий результат classifier, не его timeout. Для полного unattended
  proof нужен обычный безопасный запрос без требования literal-marker/запрета
  tools; обходить проверку или автоматически разрешать подозрительный material
  нельзя. Перед повтором используются новый candidate и подтверждённое окно без UI.
- [ ] Подготовить owner/HITL/time/remote waits и активную ограниченную команду; оператор запускает обновление через UI до их завершения. Проверить deadlines, единственный результат и сохранность данных. Команда не replay-ится; неизвестная остановка остаётся честным `SIDE_EFFECT_UNKNOWN` и barrier.

  Выполнен контролируемый restart 28 октября. Операторские логи фиксируют
  новый startup 11:31:33 UTC и application_ready 11:32:04 UTC. Owner/HITL
  continuity и таймеры C/R подтверждены, исходные receipts сохранены.
  Исходная D завершилась до restart и исключена из active-command proof.
  Свежая D2 прошла readiness START<=120 s / remaining>=360 s перед сигналом;
  новый startup произошёл до её планового окончания. Поздний API readback показывает
  `cleanup_pending`, а не подтверждённый terminal outcome. Обычный owner API
  не предоставляет stop attestation; новый экземпляр/истёкший timeout/нет файла
  не доказывают остановку старого процесса. Не replay-ить и не снимать barrier
  без точного доказательства.

  E сделала один remote Send; GetTask получил nonretryable HTTP 404 во время
  restart, operation стала failed с unknown remote outcome. R затем завершилась,
  но handle E остался failed. Исправление read classification требует отдельного
  regression proof; typed A2A TaskNotFound и Send/Cancel не должны стать blind retry.
  Локальное воспроизведение существующим wire fixture подтвердило различие:
  503 даёт Send/Get/Get → completed, замена только статуса на 404 даёт
  failed/remote_read_failed в JSONRPC и HTTP+JSON. HTTP error body сейчас
  закрывается без чтения; причина реального Cloud 404 остаётся неизвестной.
  Receipt: `cloudru-remote-404-reproduction-20261028.json`.
  После описания узкого исправления пользователь поручил продолжать;
  связанные frozen tests/spec обновляются только для различения неоднозначного
  HTTP 404 и типизированной A2A-ошибки при GetTask.
  Исправление сохранено в `948bae1`: только GetTask повторяет неоднозначный
  HTTP404 в пределах исходного poll interval/deadline; полный типизированный
  A2A error остаётся окончательным. Body ограничен 4096 bytes, проверяется полнота
  Content-Length, данные ответа не попадают в логи. Send/Cancel не повторяются.
  Targeted suite 90 tests прошёл; Ruff и spec checks 6 tests прошли; полный
  ordinary suite 2505 tests за 632,450 s, exit 0, 16 skips. Источники не менялись
  во время финального прогона; `cloudru-remote404-full-20261028.log` и
  `cloudru-remote404-source-frozen-20261028.json` сохраняют evidence.
  Новый version image опубликован и проверен отдельно (см. выше), `latest`
  не перемещался. Владелец сообщил об обновлении образа; повторная Cloud
  restart-проверка исправления ещё не выполнена. Контролируемый HTTPS fixture
  подтвердил один Send, plain GetTask 404 и последующий COMPLETED той же remote
  Task. Parent после одного разрешения собственного wait завершился полностью;
  Send/Cancel не повторялись. Точное удаление peer и остановка fixture/tunnel
  подтверждены. Это live proof обработки 404, без перезапуска Cloud и без доступа
  к private deadline: `cloudru-remote404-live-proof-20261028.json`.
  Отдельное operator evidence: `cloudru-remote404-rollout-attestation-20261028.json`.
  Временный self-peer удалён по exact ID/revision, B note удалена одним CAS.
  A note после truthful partial удалена отдельной Task
  `27254e97-ef26-4f60-9b6c-b0139d257c02`: exact revision-1 read,
  один committed CAS delete, затем marker search без результатов на index
  revision13, complete=true. Обе canary notes и self-peer очищены; исходная
  A recipe остаётся partial. Receipt: `cloudru-rollout-a-cleanup-continuation-20261028.json`.

  Дополнительные сообщения разобраны по source: `run_prefetched:125` означает
  30-секундный timeout ожидания единственного process-wide prefetch semaphore
  до запуска projection. Причина долгого удержания слота неизвестна; это не
  прямое свидетельство отсутствующего S3 object. Provider вернул embeddings 2048
  при configured 1024; векторы сохраняются, SQL search использует фактическую
  размерность, но индекс 1024 их не покрывает. Простая смена ENV на 2048 не
  доказывает HNSW index: pgvector `vector` HNSW ограничен 2000 измерений,
  при отказе создания код использует in-process cosine. Пересчёт всех прежних
  embeddings автоматически не выполняется. Источник ограничения:
  [официальный pgvector](https://github.com/pgvector/pgvector#hnsw).
  Receipt `cloudru-restart-memory-prefetch-diagnosis-20261028.json`.
  Эти отдельные проблемы не объявляются причиной D2 или remote 404. Перекрытие процессов в общих логах
  без instance identity не закрывает overlap gate. Доказательства:
  `cloudru-rollout-operator-log-facts-20261028.json`,
  `cloudru-rollout-ab-final-readback-20261028.json`,
  `cloudru-rollout-final-readback-20261028.json`,
  `cloudru-rollout-d2-late-readback-20261028.json`.
- [ ] При доступном rolling update зафиксировать перекрытие старого/нового экземпляров и проверить общие sessions, одного cron leader, workspace/execution fencing и отсутствие повторных side effects. Просто два последовательных health 200 не подтверждают overlap.
- [ ] Выполнить ограниченные target sandbox/network проверки через agent tools на собственных контрольных данных: соседний workspace/контрольные секреты, private/metadata egress и IPv4/IPv6, broker isolation, timeout/cancel/process cleanup. Native CI остаётся отдельным обязательным доказательством.
- [ ] Через UI проверить отзыв собственного тестового platform key и согласованную owner password rotation; исходные рабочие credentials не публиковать и не отзывать.
- [ ] Через доступные platform UI workflows подтвердить schema/runtime role, S3 Put/Get/Delete/limits и подготовку production. Отдельную миграцию запускать поддержанным платформой способом до serving; отсутствие такого workflow блокирует production switch, а не разрешает auto-migration существующей БД.
- [ ] На отдельной восстановленной копии подтвердить согласованный backup PostgreSQL + referenced S3 objects с отдельно сохранённым encryption key и пустым cache. Оператор использует UI backup/restore/job workflow; действующие данные не заменяются старым backup. Локальный restore proof уже есть, target restore остаётся открытым.
- [ ] Подтвердить применимые storage outage/cache-full/GC/resource-limit сценарии на контролируемой копии; injected SDK errors не выдавать за физический отказ платформы.

#### 5. Зафиксировать границу завершения

- [ ] Сопоставить финальную матрицу, ordinary/native/browser results и platform receipts с одним candidate; обновить этот план и эксплуатационные документы по фактам. Frozen implementation status менять только при разрешении и наличии требуемого обычного CI proof.
- [ ] Проверить актуальность `AGENTS.md`, сохранить reviewable commits и итоговые ссылки на image/receipts. Невыполненные platform gates перечислить явно с требуемым действием в UI.

Текущий этап завершён, когда выполнены разрешённые изменения и проверки этого
раздела. Полную готовность Cloud production нельзя объявлять по одному успешному
FM-запросу, локальному suite или пользовательскому сообщению о запуске.
Исключение telemetry из текущей работы не снимает нормативные release obligations.

## Архив исходного плана и доказательств — 24–27 октября 2026

Следующие разделы сохраняют прежние задачи, разрешения и результаты на дату
каждого этапа. Их слова «сейчас», «следующий пакет», «открыто» и исторические
checkboxes не задают актуальный список работ: он находится выше. В частности,
позднее уже исправлен kernel snapshot gap, batch4 сменился новыми images, а
полный перенос текущего main и широкое разрешение на frozen edits не предполагаются.

**Исторический статус после batch4:** после подтверждённого пользователем rollout batch3 проверены Cloud owner/HITL waits, память, peer files и завершение Qwen-задачи без повторного вопроса; её exact-format checks не прошли. 27 октября подтверждены 174 разные локальные PostgreSQL/S3/recovery проверки и ещё 56 security/config/restart проверок с указанными ниже границами. После исправления прежних fixtures полный suite batch4 v3 прошёл: 2451 tests, exit 0, 16 skips, исходники за прогон не изменились. Batch4 опубликован под version tag и `latest`, одинаковый digest подтверждён registry readback. Rollout batch4 в Cloud ещё не подтверждён. Native amd64/browser, целевые Cloud replacement/overlap и остальные release proofs не закрыты; digest работающего Cloud image публично не проверен.

**Исключение текущего этапа:** 27 октября пользователь исключил telemetry из работ. Её проверки и изменения не выполняются; прежняя неопределённость доставки не объявляется устранённой. Native `context.compacted` events остаются отдельным ожидаемым operator evidence.

Последний завершённый ordinary suite опубликованного пакета 27 октября: 2451 tests, exit 0, 16 skips; PostgreSQL,
Keycloak и MinIO работали в изолированных fixtures. Подробности нового пакета
и границы пропущенных gates приведены ниже. В исходном linux/amd64 candidate
прошли 178 config/memory/S3 tests без skips. ARM64 Landlock VM: 39 tests без
skips после повторного запуска; первая ошибка preflight не воспроизвелась,
причина не установлена, добавлена конечная приватная диагностика.
Ruff, spec lock/quality, UI tests/typecheck/build и wheel/sdist/image build прошли.
Это не подтверждение native amd64/arm64 CI и полного target Cloud release gate.

## 1. Исходная точка

Проверено по Git: `main@6434a40` целиком входит в `feat/cloudru-ai-agents@fba6edd`.
Отсутствующих коммитов main нет: результат `git rev-list --left-right --count main...feat/cloudru-ai-agents` — `0 18`.
Повторный перенос runtime, памяти, cron, MCP, skills или UI не нужен.
Перед выпуском повторяем сравнение, чтобы учесть появившиеся за время работ изменения main.

Уже реализованы и проверены в целевом Cloud-развёртывании:

| Возможность | Что подтверждено |
|---|---|
| Запуск приложения | PostgreSQL, Landlock, Python, терминал, HTTP(S), остановка команды по таймауту |
| Доступ | Вход владельца через UI; внешний чат Cloud.ru отвечает через адаптер A2A 0.3 |
| Файлы | Создание, изменение, перенос, удаление и восстановление рабочего файла после обновления Pod |
| История | Открывается после потери локального cache; устранён `STORAGE_NOT_PREFETCHED` |
| Входящие оригиналы | Скачиваются через API и настоящие кнопки браузера после удаления рабочей копии; совпадает SHA-256 |
| Результаты | Опубликованный файл сохраняет исходные байты после правок рабочей копии и обновления Pod |
| Взаимодействия | Проверены отдельные сценарии вопроса владельцу, отмены и отказа от вложения |
| Последнее исправление | 99 целевых Linux/PostgreSQL/MinIO проверок без пропусков; UI tests/typecheck/build |

Образ на исходной точке: `blockchain.cr.cloud.ru/core-agent-gn:incoming-originals-20261024`;
digest `sha256:539c38fa73bd38ba14331f09f916bb29bea2c991e8ccf0a189864835ae824f7b`.
Эти результаты закрывают перечисленные сценарии, но не полный набор возможностей main.

## 2. Согласованная граница готовности

Цель следующего этапа — поддерживаемый Cloud.ru-профиль для постоянной работы.
До этого пакета код разрешал Cloud.ru + S3 + Landlock только в `development`.
Согласовано реализовать поддержку этого состава в `production`,
с обязательными PostgreSQL/S3, стабильным ключом шифрования, проверенным sandbox
и отдельным запуском миграций. Простое удаление проверок `development` не является задачей.

Согласованные отличия от Kubernetes/main сохраняются:

| Область | Решение Cloud.ru | Что учитываем при приёмке |
|---|---|---|
| Владельцы | Общий пароль и сессии в PostgreSQL | Нет персональной Keycloak identity каждого владельца |
| Внешние агенты | Ключи выдаёт Cloud.ru; приложение использует общий внешний scope | Разные платформенные ключи не создают изоляцию peers внутри приложения |
| Исполнение | Landlock/seccomp, без новых namespaces | Это отдельный профиль изоляции со своими проверяемыми гарантиями |
| Сеть команд | HTTP(S) через проверяющий proxy | Произвольные TCP/UDP и клиенты без proxy не обещаются |
| Пути и загрузки | Реальный рабочий путь; изменяемые `inputs/` и отдельные неизменяемые оригиналы | Не обещается тождественность mount layout и syscall-поведения Bubblewrap |
| Фоновая работа | Первый поддерживаемый режим — один постоянно работающий экземпляр | Проверяем CPU вне HTTP-запросов, отсутствие scale-to-zero и краткое перекрытие экземпляров при rollout |

Если платформа не позволяет обеспечить постоянное выполнение или обязательную
изоляцию, это отдельный блокер готовности с конкретным решением на согласование.
Успешный чат не заменяет эти гарантии.

## 3. Историческая разбивка работы на этапы

| Этап | Тип работы | Что сделать | Условие завершения |
|---|---|---|---|
| 1. Закрыть конфигурационный дефект памяти | Обязательный код | При включённой памяти Cloud.ru требовать PostgreSQL в общей точке создания backend; исключить молчаливую потерю памяти из-за default `in-memory` | Неподходящая конфигурация отклоняется до serving; отключённая память допустима; PostgreSQL backend запускается |
| 2. Сделать Landlock обязательной проверкой | Обязательный CI-код | Подключить существующие native Landlock/seccomp тесты к CI с required flags; проверить выбранную платформу образа | Отсутствие enforcement приводит к ошибке gate, а не skip; сохраняется проверка Bubblewrap-профиля main |
| 3. Подтвердить все основные возможности main в Cloud | Проверки; код только по обнаруженным дефектам | Пройти матрицу ниже с реальными PostgreSQL/S3, моделью, MCP и контролируемым A2A peer | Для каждого пункта есть воспроизводимый результат, ошибки исправлены в общей точке |
| 4. Подтвердить recovery и фоновую работу | Проверки; код по обнаруженным дефектам | Idle, background, cron, ожидания, активная команда при остановке, замена Pod и rolling overlap | Нет потерянных подтверждённых данных, повторных side effects и одновременно действующих владельцев lease |
| 5. Довести эксплуатацию | Проверки и необходимые конфигурационные исправления | Миграции, runtime role, секреты/ротация, backup/restore, storage outages/GC/лимиты, telemetry | Обновление и восстановление воспроизводимы; ошибки не маскируются как успех |
| 6. Завершить production-профиль | Код и связанные контракты после этапов 1–5 | Согласованно обновить startup gates для Cloud/S3/Landlock, требования к конфигурации, spec/tests/hash lock | Валидный production-состав запускается; неполный или неподдержанный состав закрыто отклоняется |
| 7. Общая регрессия и выпуск | Проверки и документация | Полный CI, проверка main/Keycloak и Cloud, immutable tag/digest, инструкция обновления/отката, актуальная таблица отличий | Есть единый проверенный образ и список подтверждённых гарантий/ограничений |

### Этап 1. Память: подтверждённый дефект

`spec/cloudru.md` требует PostgreSQL при включённой памяти.
Сейчас `core_agent/app.py::_memory_registry` по умолчанию выбирает `in-memory`,
а запрет этого backend действует только в `production`. Работающий Cloud trial
использует `development`, поэтому ограничение обходится обычной конфигурацией.

Файлы: `core_agent/app.py`; существующие Cloud configuration и memory tests.

- [x] Добавить регрессию для включённой памяти Cloud с отсутствующим/неверным backend, корректного PostgreSQL и полностью отключённой памяти.
- [x] В общей точке startup потребовать поддерживаемый persistent backend без изменения поведения Keycloak/development вне Cloud.
- [ ] Проверить реальную запись, чтение, поиск, embeddings/NER и сохранение после замены Pod.
- [x] Сохранить изменение отдельным коммитом с соответствующим proof: `cbfca2d`.

### Этап 2. Landlock: CI и целевая среда

Файлы: `.github/workflows/ci.yml`, `deploy/landlock-native-vm.sh` при необходимости;
существующие `tests/test_landlock_policy.py`, `test_landlock_network.py`,
`test_landlock_metadata.py`, `test_sandbox_landlock.py`, `test_landlock_chat_files.py`.

- [x] Выбрать native Linux fixture для поставляемого `linux/amd64` образа с требуемым Landlock ABI; переиспользовать существующие tests и tooling.
- [x] Сделать обязательными `CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1` и `CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1` в этом gate.
- [ ] Проверить недоступность чужих workspace и секретов, запрет прямого/private/metadata egress, IPv4/IPv6, broker isolation и завершение дерева процессов.
- [ ] Отдельно подтвердить применимые свойства в целевом Cloud Pod контролируемыми ограниченными пробами.

Команда существующего набора внутри подготовленной native fixture:

```sh
CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1 CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1 \
uv run python -m unittest tests.test_landlock_policy tests.test_landlock_network \
  tests.test_landlock_metadata tests.test_sandbox_landlock tests.test_landlock_chat_files -v
```

Локальная ARM64 VM — дополнительный fixture, не замена amd64 и целевого Cloud gate.

### Этап 3. Матрица функционального соответствия main

Здесь проверяем уже существующую реализацию. Новая подсистема появляется только
при доказанной необходимости; обычный дефект исправляется в существующем общем коде.

| Группа | Сценарии, которые ещё нужно закрыть полностью |
|---|---|
| Модель и контекст | Обычные/streaming ответы, tools, reasoning settings, ограничение бюджета, compaction длинного контекста, сохранение настроек после restart |
| Память | CRUD, hybrid search, фактические embeddings/NER, shared memory по разрешениям, restart без потери данных |
| Skills | Каталог, активация и чтение ресурсов; ограничения allowlist у root и child |
| MCP | Настройка через owner UI, discovery/call, сохранение credentials, policy/HITL/deny, восстановление подключения после restart |
| Локальные агенты | `core_delegate`, background Task, wait/message/cancel, совместная папка своего чата, отдельные процессы и запрет доступа в соседний чат |
| Python/terminal | Nested `tools.call`, файлы, timeout/cancel, orphan cleanup, длительная фоновая команда, понятные ошибки неподдержанного поведения Landlock |
| Cron и ожидания | Запуск по расписанию, время/часовой пояс, owner question/HITL wait, remote wait, работа без открытого UI |
| Внешние A2A peers | Исходящие Send/Get/Cancel, повторные обращения с сохранением peer context, входящие/исходящие файлы, peer history и push там, где включён |
| Входящие A2A | Чистые карточки 0.3/1.0, built-in Cloud chat, root/external alias, owner 1.0, SSE reconnect, follow-up и dedup одного Message между входами |
| Owner UI и доступ | Настройки, история/материалы/peer panel, login/logout, expiry/rotation сессий, CSRF/Origin, отсутствие owner authority у внешнего входа |

Основные файлы уже существуют: `runtime.py`, `model.py`, `context.py`, `memory*.py`,
`mcp.py`, `skills.py`, `cron*.py`, `remote_agents.py`, `remote_operations.py`,
`a2a_sdk.py`, `a2a_compat.py`, `cloudru_auth.py`, `ui/src/`.
Результат этапа — заполненная матрица «прошло / дефект / ограничение профиля»,
а не повторная реализация перечисленных функций.

### Этап 4. Recovery, параллельность и жизненный цикл Pod

Файлы проверки/возможных исправлений: `execution_identity.py`,
`execution_reconciliation.py`, `runtime.py`, `workflow.py`, `postgres_tasks.py`,
`postgres_workspace.py`, `workspace_durability.py`, `cron_service.py`.

- [ ] Два независимых чата одновременно: свои файлы, Task, результаты и команды; соседние данные недоступны.
- [ ] Root/child в одном чате: правильная координация workspace, checkpoint и отмены.
- [ ] Закрытый браузер и длительный idle: cron, фоновые задачи и durable waits продолжают обслуживаться.
- [ ] SIGTERM/замена Pod при активной команде: команда не запускается повторно; подтверждённая остановка либо честный `SIDE_EFFECT_UNKNOWN` с действующим barrier.
- [ ] Замена Pod во время owner/HITL/time/remote wait: прежнее ожидание, deadline и ответ восстанавливаются без повторных side effects.
- [ ] Краткое перекрытие старого и нового экземпляров: общие owner sessions, один cron leader, fencing workspace writer и execution generation.
- [ ] Повторный холодный доступ к большим/смешанным вложениям, итоговым и peer artifacts; S3 I/O не удерживает PostgreSQL transaction.

Использовать отдельные тестовые чаты и контролируемые побочные эффекты.
Проверка сохранности файла после restart уже выполнена; повторяем её здесь
только как часть нового сценария активной задачи, overlap или storage outage.

### Этап 5. Эксплуатация и данные

- [x] Проверить отдельную миграцию схемы и права runtime role на disposable копии; сохранить обязательный запрет production auto-migration. Подтверждено реальной PostgreSQL LOGIN-ролью 26 октября; права действующего Cloud deployment этим не проверены.
- [x] Проверить восстановление согласованного состояния PostgreSQL + referenced S3 objects со стабильным encryption key на disposable PostgreSQL/MinIO; operator restore целевого Cloud deployment остаётся отдельным gate.
- [x] Описать откат образа/схемы: старый backup нельзя накатывать поверх новых записей без отдельной процедуры сохранения данных. Процедура и ограничения — `docs/cloudru-trial.md`, раздел подготовки production и миграции.
- [ ] Проверить недоступность/таймаут S3, заполнение локального cache, ограничения размера/числа файлов и GC без удаления referenced originals/artifacts.
- [ ] Проверить согласованную ротацию owner password и отзыв платформенного ключа на уровне Cloud ingress.
- [ ] Проверить экспорт traces/metrics/logs после исправления подтверждённого несовместимого формата Bearer у Cloud collector gateway; права настоящего ключа и успешная доставка ещё не подтверждены. Исключено пользователем из текущего этапа 27 октября: работы по telemetry не выполняются.
- [ ] Проверить readiness/liveness и записать фактические настройки ресурсов/фонового исполнения, доступные в AI Agents.

Использовать существующие `core-agent-db`, S3 ledger/GC и тесты миграций;
новый backup-сервис или новую HA-архитектуру заранее не добавлять.

### Этап 6. Завершённый Cloud production-профиль

Production configuration реализуется в общем пакете до объединённого прогона.
Допуск code path не означает release approval: реальные предпосылки Landlock,
фоновой работы и ограничения раздела 2 проверяются отдельно на одном candidate.

Файлы: `core_agent/auth.py`, `core_agent/app.py`, `core_agent/sandbox_landlock.py`;
связанные `spec/cloudru.md`, `spec/releases/v1.md`, `spec/acceptance.md`,
`spec/security-and-reliability.md`, Cloud config tests и `tests/test_spec_lock.py`.

- [x] Согласованно задать поддерживаемое сочетание Cloud auth + PostgreSQL + S3 + Landlock для production.
- [ ] Обязать стабильные secrets, правильные storage roots, schema/runtime role и проверенный sandbox до readiness.
- [x] Сохранить отказ при ephemeral storage, недоступном sandbox/БД, неверных credentials/settings и неподдерживаемой комбинации режимов.
- [ ] Проверить прежний Keycloak/Bubblewrap профиль main и отсутствие автоматического переключения профилей при ошибках.
- [x] Удалить устаревшие сообщения о том, что S3 ещё не реализован; диагностика должна отражать реальную причину отказа.

### Этап 7. Общая регрессия и выпуск

- [x] Вновь сверить ancestry main и Cloud-ветки; новые main-исправления включать осознанно и проверять совместимость с адаптацией.
- [x] Для опубликованного пакета поднять disposable PostgreSQL, Keycloak, MinIO и выполнить полный ordinary suite и packaging/image checks.
- [ ] Выполнить оставшиеся native/security/browser gates на требуемых fixtures; новые изменения после опубликованного пакета проверить отдельно.
- [ ] Зафиксировать proof по каждому пункту матрицы; обновить implementation status только там, где доказательство действительно получено.
- [x] Обновить `AGENTS.md`, `docs/cloudru-trial.md` и ENV-инструкцию по фактическим границам профиля.
- [x] Подтвердить наличие итогового документа сравнения Kubernetes/Cloud.ru: `Desktop/Core_Agent_Kubernetes_Cloudru_Comparison.md` и `.docx`; наличие обоих артефактов проверено 27 октября. Постоянное сравнение не переписывается ради текущего статуса suite/image.
- [x] Опубликовать проверенный batch4 под постоянным version tag и digest; version tag и `latest` подтверждены registry readback 27 октября.
- [ ] После сообщённого оператором rollout batch4 проверить целевой endpoint; предыдущая live-проверка относилась к batch3.
- [ ] Закрыть оставшиеся release proofs; HTTP API не подтверждает точный digest запущенного Pod.

Базовые команды существующих gates:

```sh
uv run ruff check core_agent tests
uv run python -m unittest discover -s tests -v
```

Из `ui/` с Node.js 24:

```sh
npm ci
npm test
npm run typecheck
npm run build
```

Эти команды требуют настроенных fixtures; отсутствующая инфраструктура или
skip обязательного native gate не считаются подтверждением готовности.

## 4. Согласованный пакетный порядок

1. Собрать все необходимые изменения кода, CI, spec/acceptance, regression tests,
   frozen hashes и эксплуатационных документов этапов 1–2/5–6 в рабочей ветке.
2. Выполнить один объединённый набор затронутых и обязательных regression/native/
   browser/package gates. Исправления выявленных дефектов включить в этот пакет;
   повторять только проверки, необходимые после изменений или failures.
3. Собрать и опубликовать один candidate image с постоянным tag/digest. Передать
   единый rollout checklist; не обновлять Cloud deployment после каждой правки.
4. Проверить этапы 3–5/7 на этом candidate. Существующий Cloud development
   deployment сохраняет tenant, DB/S3 references и secrets; production switch
   требует подготовленной отдельной migration и стабильного encryption key.
   До этих условий candidate проверяется с действующим development profile.
5. Зафиксировать реальные proofs и оставшиеся blockers. Code/CI support
   production не объявлять завершённым release без target idle/security/recovery.

Рабочая ветка остаётся `feat/cloudru-ai-agents`. Пользователь разрешил связанные
изменения spec, acceptance, tests и frozen hashes для согласованных доработок.
План не требует пересоздания проекта или изменения параметров кластера Cloud.ru.

## 5. Результаты общего live-прогона 24–25 октября 2026

Проверки выполнены на существующем Cloud deployment после подтверждённого
пользователем rollout. Ожидаемый candidate — `cloudru-batch-20261024`, index
`sha256:35609371e1aab984f4f68c987fe35e92a0d1059ec23da1900580399e615a6dcc`.
HTTP API не раскрывает revision/digest, поэтому точный запущенный image независимо
не подтверждён. Это manual deployment proof, не основание менять CI implementation status.

| Область | Полученное доказательство | Остаток / ограничение |
|---|---|---|
| Входящий A2A и доступ | 35 проверок: карточки 0.3/1.0, root/alias dedup, owner-only маршруты, Origin, запрет перечисления external Tasks; обе тестовые задачи завершились точными ответами | SSE reconnect, rotation/expiry и rolling overlap проверяются отдельно |
| Owner UI | Настоящий Chromium: вход, чтение своей истории, отображение результата, история после reload, отсутствие JS errors, logout → identity 401 | Не заменяет обязательный native browser/Keycloak gate |
| Cron и время | Создание, повтор запроса, ручной запуск без дублирования, CAS, disable/delete; новый Message прерывает timer, повтор Message не создаёт новый запуск | Автоматическое расписание при длительном idle ещё не доказано |
| Idle | После подтверждённого закрытия UI — 300 секунд без наших HTTP-запросов. Timer проснулся в 19:57:30.834592 UTC; первое чтение после паузы в 19:58:15 UTC уже увидело COMPLETED | Доказывает один пятиминутный интервал; не гарантия отсутствия scale-to-zero или рестартов платформы |
| Память и child | Create/read/update rev1→2, точный vector result, child с allowlist search/read прочитал ту же запись; delete committed и последующий поиск пуст | NER вернул degraded graph «1 document(s) not extracted». Причина требует безопасного provider error code из журнала; restart памяти отдельно |
| Skills и исполнение | Активация skill с digest, Python `tools.call` → terminal, отсутствие пяти проверенных secret ENV, отказ чтения своего canary из соседнего чата с EACCES, timeout → cleanup → следующая команда | Не доказывает весь сетевой профиль, resource limits или остановку при смерти Pod |
| Background cancel | Создание фоновой команды и mailbox message подтверждены | После разрешённого cancel root завершился SIDE_EFFECT_UNKNOWN. Публичных данных недостаточно для определения cleanup/lease причины; повторный запуск команды не выполнялся |
| Исходящий peer | Карточка self-peer совместима с pinned A2A 1.0 alias | Сохранение подключения с ключом вернуло 503 REMOTE_AGENT_CREDENTIAL_UNAVAILABLE. Подключение/задача не созданы; требуется настроенный стабильный encryption secret |

Исходные безопасные receipts находятся рядом с worktree в
`.enterprise-work/cloudru-batch-live-*-20261024.json`: protocol, control-v2,
tools, memory-child, browser-v2, idle и peer. Первые control/browser попытки
содержат ошибки тестовой подготовки (несуществующий chat ID, чтение timer через
owner interactions, строковый JS eval при CSP); они не считаются product defects.
Сетевые ReadTimeout/ConnectTimeout сохранены как наблюдения без догадки о причине.
Все собственные model roots завершены либо отменены; тестовые memory записи,
canary-файлы и временное расписание удалены. Общие policies/settings не менялись.

### Пакет исправлений по итогам прогона

Отдельно подтверждены два дефекта общей реализации отмены: безопасный отказ
до dispatch ошибочно превращался в unknown; cleanup wrappers теряли конечную
приватную SandboxDiagnostics. Исправление сохраняет прежние public error
codes, отличает pre-dispatch от callback после cancel и оставляет uncertain
side effects закрытыми. В частности, `TOOL_ARGUMENT_INVALID`/`TOOL_START_FAILED`
после dispatch не разрешают продолжить model loop. Оно не устанавливает причину
упомянутого live incident.
Дополнительная ARM64 native regression с реальным child, TerminalSessionManager
и concurrent reaper прошла; исходный Cloud сбой в ней не воспроизведён.
На итоговом коде прошли 205 targeted tests с отдельной PostgreSQL БД и 383 tests
в собранном linux/amd64 candidate с PostgreSQL/MinIO, без skips; независимое
повторное review блокеров не выявило. Это не заменяет native sandbox gates.
Общий ordinary suite на итоговом коде: 2410 tests, exit 0, 16 платформенных skips,
с настоящими PostgreSQL, Keycloak и MinIO. Ruff, frozen spec lock и wheel/sdist
build прошли. Пропуски не засчитываются как прохождение соответствующих gates.

Следующий rollout объединяет исправления и настройку encryption secret.
После него повторяются failed/blocked случаи: active background cancel с точной
диагностикой, peer credentials + Send/Get/Cancel, provider NER. Для MCP нужен
контролируемый доступный HTTPS fixture; для compaction — provider + две реальные
compaction receipts; для production — runtime DB grants, coordinated backup,
native amd64/arm64 gates и один согласованный restart/overlap сценарий.

### Повторный Cloud-прогон 25 октября и причина S3 cancel race

После rollout `cloudru-batch-fixes-20261025` безопасный отказ отмены до dispatch
подтверждён в Cloud: `TASK_NOT_FOUND` сохранился в tool receipt, следующая
terminal-команда выполнилась, root завершился `COMPLETED`.
Отмена работающего background `sleep` повторно дала `SIDE_EFFECT_UNKNOWN`.
В предоставленных оператором логах перед этим виден `LEASE_LOST` при checkpoint
child; доказательств timeout остановки процесса в этих строках нет.

Локальная детерминированная проверка с PostgreSQL выявила расхождение общего
workflow store и S3 workspace coordinator: `confirm_execution` сохраняет
подтверждение остановки без изменения версии run, lease token или generation.
S3 guard сравнивал весь прежний `execution_owner` с новым receipt и отвергал
checkpoint известного результата как `LEASE_LOST`. Обычный workflow transition
с теми же record/token принимает результат и сохраняет актуальный receipt.
Это воспроизведение дефекта компонента; приватное состояние live lease из
публичного API не считывалось.

После отдельного добавления стабильного `PUSH_NOTIFICATION_ENCRYPTION_KEY`
обе health-проверки вернули 200. Создание собственного тестового peer с
API-ключом прошло, `has_header_value=true`: прежний configuration blocker
`REMOTE_AGENT_CREDENTIAL_UNAVAILABLE` снят. Функциональные Send/Get/Cancel
пока не подтверждены: собственный root оставался `WORKING` 610 секунд без
tool receipts и без исходящих операций. Один cancel-запрос завершился
`ReadTimeout`; последующее чтение подтвердило `CANCELED`, запрос не повторялся.
Собственное подключение удалено и отсутствие проверено, чужие настройки не
менялись. Причина остановки продвижения root требует deployment logs, это ещё
не доказательство ошибки исходящего A2A transport.
Первая отправка не задавала `returnImmediately` и использовала blocking default
SDK; HTTP timeout сам по себе не доказывает сбой admission. Повтор с явным
`configuration.returnImmediately=true` получил Task за 1,35 секунды, но снова
не дал tool/outgoing receipts за 312 секунд. Собственный root затем подтверждён
как `CANCELED`, собственный peer удалён с readback. Простой отдельный запрос без
инструментов вернул точный контрольный ответ за 101,63 секунды от начала пробы;
его timed-out blocking Send не повторяли, результат нашли по исходному context.

Безопасные receipts: `.enterprise-work/cloudru-batch-live-cancel-20261025.json`,
`cloudru-cancel-ownership-repro-20261025.json` и
`cloudru-batch-live-peer-key-20261025.json` рядом с worktree.
Следующий пакет исправляет совместимость checkpoint с монотонным receipt
подтверждённой остановки; tenant/scope/version/live lease/generation проверки и
`ABORTED` для неизвестного результата orphan execution сохраняются.
Целевое воспроизведение исправления: 14 тестов без skips, включая реальный
процесс с PostgreSQL/MinIO, сохранённый результат отмены и восстановление файла
после потери локальной рабочей копии. Независимое review блокеров не выявило.
В собранном linux/amd64 candidate прошли 409 тестов без skips. Полный ordinary
suite: 2414 тестов, exit 0, 16 платформенных skips с отдельными PostgreSQL,
Keycloak и MinIO. Первый общий прогон выявил гонку прежнего timer-теста:
assertion о `working` выполнялся до запуска child thread. Детерминированное
воспроизведение подтвердило этот порядок; тест теперь ждёт `started` Event,
сохраняя проверки отмены и завершения. Повторный полный прогон прошёл.
Ruff, frozen spec lock, diff check и wheel/sdist build прошли. Native release
gates остаются отдельными; результат повторной отмены после rollout приведён ниже.

Опубликован один новый candidate под тегами
`blockchain.cr.cloud.ru/core-agent-gn:cloudru-s3-cancel-20261025` и `latest`:
index `sha256:ff6031cd5400bffb86df1389f265f2563fad04a8254bf11c302a6dda85aafa3a`.
Readback registry подтвердил одинаковый index обоих тегов и совпадение runtime
amd64 manifest/config с проверенным локальным образом. Общие результаты и
digests сохранены в `.enterprise-work/cloudru-s3-cancel-20261025.json` рядом
с worktree; дополнительные live receipts — `cloudru-batch-live-peer-key-v2-20261025.json`
и `cloudru-batch-live-model-control-20261025.json`. Собственные regression fixtures
остановлены; существующие пользовательские контейнеры не менялись.

Дополнительные NER/summary probes использовали другой локально настроенный
DeepSeek endpoint, а не Cloud Qwen. Четыре вызова не получили HTTP-статуса;
отдельно обнаружено отсутствие CA в локальном Python default TLS context.
Эти попытки не подтверждают несовместимость модели и не засчитываются как
проверка deployed NER/compaction; обход TLS не применялся.

### Повтор после rollout S3 cancel и следующий пакет Card discovery

После подтверждённого пользователем rollout проверены readiness и owner login.
Новый root `128059a8-c9dd-4231-a325-cd76384a9cbf` принят с явным
`configuration.returnImmediately=true`. Единственный background `sleep 300`
`76ba5deb-9c27-5fa4-ab2a-ad23e7412412` имел `working` в реальном `core_task_get`
до отмены и `canceled` в последующем `core_task_wait`. Все шесть tool receipts
успешны; после отмены terminal вернул точный marker, exit 0 и
`cleanup=sandbox_terminated`, root — `TASK_STATE_COMPLETED`.
Проверены persisted receipts, не только текст модели; прямого доступа к OS
handles child и digest работающего Pod нет. Новых политик, peers и файлов этот
тест не создавал. Отчёт: `cloudru-live-s3-cancel-20261025-v2.json` рядом с worktree.

Локальное ограниченное воспроизведение отдельно выявило цикл исходящего A2A:
при чтении собственной Agent Card `card_skills` вызывал `_remote_peers`, а
discovery self-peer повторно обращался к той же Card до model request.
Нормативное исправление разделяет metadata capability Card и фактическую
доступность адресатов в model catalog/dispatch; wire versions, credential checks
при вызове и tenant boundaries сохраняются. Это доказанный локальный дефект,
но private logs прежних Cloud зависаний пока не подтверждают, что там был
именно этот путь. Результат проверки после отдельного rollout приведён ниже.

Regression proof: три новых теста сначала воспроизвели нарушение, затем прошли.
Проверены self-peer без вложенного discovery, сохранение настоящего runtime
discovery, обе карточки owner/external, tenant/policy/platform ceiling,
disabled/deleted peers и enabled запись после первой страницы. Карточки не
читают credentials; ранее принятые reply bindings остаются доступны runtime.
Независимое review блокеров не выявило.

Полный ordinary suite: 2417 тестов, exit 0, 16 dedicated native/browser skips,
563,189 секунды с реальными PostgreSQL 17, Keycloak и MinIO. В собранном
linux/amd64 candidate прошёл 251 remote runtime/auth/Cloud/transport/registry/
operations/progress/spec-lock тест без skips на отдельной тестовой БД и bucket.
Ruff, шесть spec lock/quality проверок, diff check, wheel/sdist и image build
прошли. Публикация и digests candidate фиксируются в
`cloudru-card-discovery-20261025.json` рядом с worktree; полный лог —
`cloudru-batch4-full-suite-20261025.log`. Эти проверки не заменяют native release
gates; фактический исходящий Send/Get/Cancel проверен отдельно после rollout.

### Cloud Send/Get/Cancel после rollout Card discovery

25 октября, 19:06–19:10 UTC: readiness и карточка вернули 200, собственный peer
с API-ключом сохранился. Root `0019261a-23ef-48d4-960e-c769b9b9e341` принят за
0,64 секунды с `returnImmediately=true`. Единственный реальный
`core_agent_send_message` создал operation `d1e4516d-a646-5006-ae05-800cfc654865`;
её `request_delivery=confirmed`. Пока root ждал точный ответ владельца и до
отмены, `last_checked_at` той же операции вырос с 1792955303.982321 до
1792955347.295846: подтверждён отдельный worker Get, а не только локальное
чтение scheduler snapshot.

После ответа владельца выполнены по одному `core_task_cancel` и `core_task_wait`.
Четыре tool receipts успешны, root — `TASK_STATE_COMPLETED`, operation —
`canceled`, remote — `TASK_STATE_CANCELED`, `outcome_unknown=false`, ошибок нет.
Собственное подключение удалено; registry readback подтвердил его отсутствие.
Повторных model Send/Cancel и повторной отправки root не было, общие настройки
и чужие подключения не менялись. Report со всеми receipts:
`cloudru-batch-live-peer-key-v3-20261025.json` рядом с worktree.

Это proof протокольного Send/Get/Cancel через реальный ingress. Получатель
находился в owner wait; запуск его `core_wait_until` и отмена активного таймера
этой проверкой не доказаны. Live image digest не доступен через публичный API;
проверка выполнена после подтверждённого пользователем rollout.

### Пакет MCP и NER 25 октября

| Проверка | Фактический результат | Граница доказательства |
|---|---|---|
| MCP в Cloud | Owner settings приняли отдельный временный сервер Microsoft Learn; единственный `microsoft_docs_search` вернул успешный receipt, root завершился `COMPLETED`. Собственные input/HITL approvals применены к точным материалу и вызову. | Реальный discovery/call через Cloud, без credentials на публичном сервере; controlled auth/negative/SSE сценарии в Cloud этим не проверены. |
| Очистка MCP | CAS-обновление удалило только собственное подключение; readback подтвердил пустой исходный список и восстановленное наследование. Другие поля settings не записывались. | Одно подключение, без конкурентной смены владельцем того же списка. |
| NER и hybrid search в Cloud | Собственная session-заметка с полностью строчными title/body найдена по `postgresql`: `graph=1`, `vector=0.4601315122803613`, `degraded_channels=[]`. Начальный поиск пустой. | Встроенный regex извлекает только слова с заглавной буквы: этот результат подтверждает model-derived grounded entity по текущему code path. Прямого provider trace и оценки качества остальных сущностей нет. |
| Очистка памяти | Ровно шесть успешных receipts: search × 3, create/read/delete × 1. Удалён только созданный `memory_id` с revision 1; `committed=true`, повторный поиск пустой, root `COMPLETED`. | Подтверждено логическое удаление своей заметки; физическое удаление истории и сохранность NER после замены Pod не проверялись. |
| Локальная регрессия | MCP: 71 тест и ещё 2 PostgreSQL проверки; память: 59 тестов с PostgreSQL; контекст: 50 тестов. Все итоговые прогоны завершились с exit 0 без skips. | Это targeted proofs; они не заменяют полный native/browser release gate. |

MCP root: `22a87d39-cec7-4f22-bd18-02673d16adfc`; NER root:
`9b5515cd-dbc3-4971-b181-84435506ea11`. Безопасные отчёты рядом с worktree:
`cloudru-mcp-review-20261025.json`, `cloudru-mcp-live-20261025.json`,
`cloudru-ner-live-20261025.json`; memory log:
`cloudru-memory-ner-local-20261025-retry.log`.
Первый PostgreSQL прогон после старта fixture использовал устаревший динамический
порт и упал; повтор с портом из Docker readback прошёл. Пользовательские
контейнеры и deployment environment не менялись; собственный fixture остановлен.

Отдельная изолированная попытка summary с локально настроенным DeepSeek и
проверенным CA дошла до HTTP 404 за 3,068 секунды. После первого отказа
остановлена: один запрос, ноль compactions, без повторов и без изменений Cloud.
Отчёт `cloudru-batch-compaction-provider-20261025.json` не является проверкой
deployed Qwen и не устанавливает дефект алгоритма сжатия.

### Вложения, SSE и права runtime-роли: пакет 25–26 октября

| Проверка | Фактический результат | Граница доказательства |
|---|---|---|
| Raw FilePart между версиями | Внешний A2A 0.3 принял собственный файл 75 bytes; идентичный Message через внешний 1.0 alias вернул ту же Task. Одна исходная запись, одно вложение, ноль tool calls, `COMPLETED`. | Один малый текстовый файл; Pod replacement и storage outage здесь не выполнялись. |
| Оригинал и валидация | Owner API вернул original с `no-store`, точными bytes/SHA-256 и provenance `a2a/`; noncanonical base64 `YR==` отклонён как `INVALID_FILE_ENCODING`. | Owner authority сохранена; отрицательная проверка внешнего доступа к original в этом пакете не повторялась. |
| Разрыв SSE | Одна внешняя Task, один Send: 0.3 subscription получила working event и была закрыта клиентом; подписка 1.0 через alias получила продолжение и `COMPLETED`, история содержит один исходный Message и точный ответ. | Подтверждён клиентский disconnect/resubscribe; это не замена Pod. |
| Production runtime grants | Реальная отдельная LOGIN-роль после migration и успешного schema `check` не могла писать в `core_workspace_manifests` и `core_s3_blobs`. Regression воспроизвела две `InsufficientPrivilege`; добавлены только `SELECT, INSERT, UPDATE`. | Для уже существующей БД нужен повторный запуск обновлённого migration CLI с `DATABASE_APP_ROLE`; один rollout права не обновит. |
| Проверка исправления | 11 migration/spec тестов без skips, включая настоящий dump/restore; проверены разрешённые DML и запреты DELETE/TRUNCATE/ALTER/CREATE. Ruff и независимое review прошли. | Backup gate покрывает PostgreSQL и локальные test blobs, а не согласованное восстановление Cloud PostgreSQL + S3. |
| Docker restart | Существующий lifecycle test прошёл на amd64 candidate image в локальном ARM64 Docker. | Эмуляция не закрывает native amd64/arm64 matrix. |

Raw FilePart root: `4e6f5716-cd5e-4d86-b9ac-97a8888b8ada`; SSE root:
`f8bb2136-d6cf-4528-b44f-ddf6bc98d1fa`. Receipts:
`cloudru-rawfile-alias-live-20261025.json`,
`cloudru-batch-sse-resume-20261025.json`, `cloudru-release-gates-20261025.json`.

Операторские логи compaction probe `057a6c52-6e4b-4767-a178-ad9ac4b1c13a`
подтвердили успешную обработку первого ответа владельца и переход в RUNNING.
Затем проверочный скрипт отменил задачу после одного неудачного чтения API;
`task.canceled` содержит `requested=true`. Compaction в этих логах отсутствует:
это незавершённая проверка, а не доказанный дефект сжатия. Следующий драйвер
повторяет только чтения при transient errors и сохраняет точную причину отказа;
мутации автоматически не повторяет.

В предоставленных операторских логах OTEL получает HTTP 403. Локальные 15
observability tests прошли; на том этапе причина отказа collector ещё не была
установлена. Формат credentials и telemetry тогда не менялись. Позднейшая
проверка gateway без настоящего ключа описана ниже.

Автоматический cron проверен 26 октября отдельным расписанием с единственным
ближайшим annual occurrence: due `06:41:00 UTC`. Между `06:38:39.889406` и
`06:42:30.003407 UTC` наши клиенты не отправляли запросов к агенту. Первое чтение
нашло один automatic Message и задачу `d3aa9d0e-f1b9-4db2-8552-81305f27f320`,
уже перешедшую в рабочее состояние в `06:41:04.952821 UTC`; исходный материал
ожидал guardrail review. После разрешения только точного собственного текста
задача завершилась ожидаемым ответом. Собственное расписание удалено, GET
подтвердил 404. Это proof автоматического admission во время одного тихого
интервала; завершение потребовало owner review, а отсутствие всех платформенных
HTTP probes и scale-to-zero этим не доказано. Первичный script filter пропустил
guardrail placeholder; отдельная reconciliation сохранила исходный результат
и установила Task по automatic Message ID. Отчёт:
`cloudru-cron-idle-20261026.json`.

На source `1eb06a5` существующий `deploy/landlock-native-vm.sh` прошёл 40 tests
без skips с обоими required flags: ARM64 Linux 6.12.113, ABI 6. Ещё один native
ARM64 Docker restart test прошёл. Использованы закреплённый ARM64 rootfs и
текущий source overlay; это не проверка всего содержимого опубликованного amd64
candidate и не замена Bubblewrap/Pod/browser matrix. Временные VM/контейнеры
удалены; отчёт `cloudru-native-head-20261026.json`.

В обычный suite добавлена одна regression
`CloudStorageRuntimeTests.test_quiesced_backup_restores_referenced_s3_generations_and_encrypted_peer`.
Она останавливает собственных writers, сохраняет PostgreSQL schema через
настоящий `pg_dump` и копирует только referenced S3 generations собственного
tenant. Затем удаляет исходные objects/schema/local cache, восстанавливает БД
через `pg_restore` и подтверждает ошибку `NOT_FOUND` до возврата S3 objects.
После возврата objects сверяет точные bytes опубликованного файла и двух файлов
workspace, включая `.git/config`, а также состояние БД и encrypted peer.
Расшифрованный SQL dump не содержит key/plaintext credential; старый key работает,
другой key даёт `REMOTE_AGENT_CREDENTIAL_UNAVAILABLE`. Новых model calls нет.
Targeted run: один тест, exit 0, без skips; независимое review блокеров не нашло.
Это локальный quiesced restore proof до новых writes, не восстановление
production backup поверх продолжившей работать базы.

### Проверка общего workspace и итоговый ordinary suite 26 октября

Root `e63d4255-419e-4371-a930-cfdb0e0178f0` создал собственный файл, child
`482ea6dd-d707-4348-b56f-e18d31259804` прочитал его и добавил свою строку.
Повторное чтение родителем, immutable artifact и owner download совпали:
37 bytes, SHA-256
`1c82e8c59d4cbe5200bbd3b02285827df04eec4e59c5a297aa6817fe8bd5287d`.
Обе задачи завершены, четыре root tool calls и один child call успешны,
настройки не менялись. Это live proof общей папки и выдачи файла, не замена
Pod во время child execution. Receipt: `cloudru-child-workspace-live-20261026-v2.json`.

Полный `uv run python -m unittest discover -s tests -v` завершился за
658,497 секунды: 2419 tests, exit 0, 16 skips. Первый прогон выявил две гонки
fixtures: фоновый recovery успевал обработать намеренно оставленное pending
состояние раньше явного вызова теста. Перед такой проверкой исходный poller
теперь останавливается и дожидается завершения; реальные recovery paths и
assertions сохранены, новая app после restart запускает свой poller.
Targeted regression и повторный полный suite прошли; независимое review
не обнаружило ослабления проверок. Ruff и diff-check также прошли.
Receipt: `cloudru-coordinated-backup-proof-20261026.json`.

Пропуски относятся к Linux SGID, native sandbox/seccomp, Docker restart,
настоящему browser/Pod fixture и одному memory-only варианту cron loop.
Отдельные ARM64 Landlock/restart proofs выше не заменяют весь native amd64/arm64
CI. Публичный `origin/main` повторно получен: `6434a40` по-прежнему полностью
входит в ветку. Публикация Git-ветки для CI пока недоступна: GitHub push требует
отсутствующей локальной аутентификации. Это не успешный native CI gate.

### Открытые диагностики текущего пакета

Новая bounded compaction Task `6023385d-09ff-4533-8735-c734c2d90dcf`
(run `9e7420e9-80ef-428a-b2a1-248e7eae4742`) завершилась
`CONTEXT_UNRECOVERABLE` 26 октября. Проверочный клиент её
не отменял; все 32 чтения вернули HTTP 200. Временное окно 20480 было закреплено
в Task, а owner setting сразу восстановлено до прежних 128000, revision 11.
Операторские логи уточнили время terminal commit — `06:53:32.967551 UTC`;
`06:53:58 UTC` было временем первого чтения итогового состояния клиентом.
Обе попытки summary дали `provider_unavailable` при working capacity 9617;
вторая заняла около 50 секунд. Этот общий код adapter также обозначает
invalid response и отсутствие public text, поэтому он не доказывает timeout.
Пакет добавляет конечную приватную классификацию причины с проверенным HTTP
status/exception/response format/finish reason; публичная ошибка, strict schema,
лимиты и reasoning settings сохраняются. Изменение лимитов без этой диагностики
не считается исправлением. Receipt:
`cloudru-batch-compaction-questions-20261026-v3.json`.

Операторский startup log `08:23:15–17 UTC` подтверждает успешный запуск с S3
и Landlock, один `/v1/traces`, `OTEL_API_KEY=missing` и
`OTEL_ENDPOINT_API_KEY=set`. Следовательно, используется платформенный
fallback key без перекрытия основным ключом. Эти строки не подтверждают его
права. Более поздние операторские логи `11:43–11:47 UTC` подтверждают 403
после следующего rollout. Сами startup-логи не определяют причину отказа;
отдельная проверка gateway ниже установила несовместимость формата Bearer.

В Cloud также проверен запрет чтения между собственными тестовыми чатами:
Task `f743cfbc-f538-4058-96ec-c7ff37b6dd8c` получила `PermissionError/EACCES`
при попытке прочитать ровно один известный canary соседнего чата. Собственный
файл записан и прочитан с точными bytes/SHA; canary соседа до и после совпал.
Общие настройки и политики не менялись. Это проверка одного scoped path,
не одновременных writers или rollout. Receipt: `cloudru-chat-isolation-20261026.json`.

Содержимое amd64 candidate `sha256:47a59c1de29a7b8128b91365b96b7ae573c7bd3ea54f1301771e022e2e87e2bd`
проверено в отдельном x86_64 Linux guest QEMU TCG: 40 Landlock/seccomp tests,
39,192 секунды, exit 0, ноль skips, оба required flags. Runtime взят из точного
rootfs образа без source overlay. Guest исполнялся на ARM64 Docker host;
это emulated amd64 proof, не native hardware CI и не профиль Cloud host.
Fixture и rootfs containers удалены. Receipt: `cloudru-landlock-amd64-proof-20261026.json`.

Повторные обращения к собственному тестовому A2A peer подтвердили server-owned
remote context binding: второй remote Task прочитал файл первого без явного
`context_id` в model arguments. Оба remote Task завершились; исходный файл и
повторно опубликованный результат совпали, 25 bytes. Первый parent получил
результат; второй был отменён проверочным клиентом по его deadline до следующего
Get. Поэтому полная повторная доставка parent в этом прогоне не подтверждена.
Все созданные этим сценарием задачи сверены, собственный peer удалён.
Receipts: `cloudru-peer-context-files-20261026-v3.json` и
`cloudru-peer-context-files-reconciled-20261026.json`.

Разбор выявил общий дефект: ранний artifact нетерминального remote Task обходил
адаптивный polling и назначал следующий Get через pinned interval (300 секунд).
Теперь обе ветки используют один расчёт 10/30/300 секунд, observation interest
и deadline, сохраняя атомарную публикацию quarantine и единственный Send.
Regression воспроизвела исходный дефект; 208 целевых Memory/PostgreSQL тестов
прошли без skips. После уточнения timing assertion два затронутых Memory/PG
сценария также прошли. Полная регрессия опубликованного пакета и успешная
live-проверка повторной доставки v5 описаны ниже.
Receipt: `cloudru-early-artifact-poll-proof-20261026.json`.

В том же пакете HTTP adapter перестал читать тело уже отклонённого HTTP-ответа:
ошибка чтения могла маскировать известный status сырым transport exception.
Ответ закрывается; прежние `MODEL_UNAVAILABLE` и retryable-классификация status
сохраняются. 141 целевая проверка HTTP/model/runtime прошла без skips, включая
OpenAI/Anthropic, streaming и ошибку закрытия. Это локальное доказательство
обработки ошибок, а не установление причины прежнего Cloud compaction failure.

### Проверенный пакет диагностики и polling 26 октября

Финальный ordinary suite опубликованного пакета: 2424 tests за 577,198 секунды,
exit 0, 16 skips; Ruff и diff-check прошли. Использованы новые изолированные
PostgreSQL database и S3 bucket, реальные Keycloak/MinIO. Предыдущий прогон
выявил ещё одну гонку test fixture: periodic recovery забирал lease у явного
`resume_task` и тест получал `LEASE_LOST` вместо ожидаемого
`SIDE_EFFECT_UNKNOWN`. Остановка и join scanner до подготовки прерванного
состояния устранили гонку без изменения production recovery или assertions;
11 целевых тестов и полный повторный прогон прошли.
Receipt: `cloudru-provider-remote-final-retry-20261026.json`.

15 skips требуют Linux/native Docker/browser fixtures; ещё один намеренно
не повторяет PostgreSQL leader ownership проверку в memory-only loop fixture.
Они не объявляются пройденными native CI gates. Новый linux/amd64 image index
`sha256:b3ca335bd3823742c41bb7098857c75250627e10a8fd22c961a99384a42f677b`
прошёл 40 Landlock/seccomp tests без skips за 38,722 секунды в x86_64 QEMU TCG
VM на ARM64 host, с обоими required flags и точным rootfs без source overlay.
Все 73 application Python-файла образа совпали с проверенной рабочей копией.
Wheel/sdist также собраны. Receipt: `cloudru-batch2-candidate-proof-20261026.json`.

Пакет опубликован как
`blockchain.cr.cloud.ru/core-agent-gn:cloudru-validation-batch2-20261026`
и `blockchain.cr.cloud.ru/core-agent-gn:latest`. Оба registry index совпали
с указанным проверенным image digest; native manifest/config также сверены.
Оператор сообщил о rollout около `11:39 UTC` 26 октября; последующие проверки
целевого endpoint приведены ниже. HTTP API не раскрывает revision/digest Pod,
поэтому соответствие фактически запущенного Pod этому digest независимо не подтверждено.

### Результаты после rollout 26 октября

AB v2 завершён успешно в `11:42:41 UTC`: прежний owner cookie без повторного
login сохранил доступ, company settings совпали. В A сохранился исходный owner
question с теми же wait/generation/deadline/digest; после ответа прочитана и
найдена та же memory-запись с прежними ID/body/revision. Собственная запись
удалена, последующий поиск пуст без degraded channels. В B сохранился точный
HITL на memory-create; после одного approval выполнены create/read/delete.
Python/terminal уже имели policy allow, поэтому это не proof sandbox approval.
Общие policies не менялись, cookie оставался только в памяти клиента; logout
вернул 200. Receipt: `cloudru-rollout-ab-20261026-v2.json`.

CD reconciliation подтвердил нормальное завершение 20-минутного timer
(`woke_at=11:15:28.049456 UTC`) и восьмиминутной background-команды
(exit 0, duration 480,2279 секунды, cleanup `sandbox_terminated`). Оба завершились
до сообщённого rollout. После rollout сохранённый task-wait HITL родителя был
разрешён один раз; новый запуск команды не выполнялся. Canary после обновления
доступен: 28 bytes, прежний SHA-256
`81969ecf795e619d0814e69b3bf7d6de0dd39f42acb19b4d33f9d96e0c282ba4`.
Это proof нормального завершения и сохранности файла, но не ожидания таймера
через rollout, прерывания активной команды или подтверждённой остановки
прежнего process generation. Эти gates открыты. Receipt:
`cloudru-rollout-cd-reconciliation-20261026.json`.

Peer v5 завершён в `11:58:48 UTC`: обе операции и оба parent/remote Task
завершились, remote Tasks различны и используют один server-owned context.
Первая операция создала файл, вторая только прочитала его; model arguments
не задавали context ID. Оба parent download вернули 200/no-store и точные
25 bytes с SHA-256
`9b3adc80271b6bdc3b4252c2d44a96a263eb697bbe4d83010cf12bbe2225e815`.
Все семь proof-полей receipt истинны; собственный peer удалён (200), logout 200.
Это закрывает повторную доставку файла из того же remote context. В предыдущем
v4 первая фаза прошла, но driver ошибочно отклонил разрешённый
`core_response_begin`; это не product failure. Receipt:
`cloudru-peer-context-files-20261026-v5.json`.

### Сжатие контекста и telemetry: оставшиеся ограничения

В v4 Task `2983f413-8f7e-46ad-9d5f-6fa2377b51f6` обе попытки summary
завершились с `provider_failure={kind:no_text, finish_reason:length,
response_format:json_schema, exception_type:CoreError}`, working capacity 9102.
Terminal commit `11:47:05.567502 UTC`: `CONTEXT_UNRECOVERABLE`,
reason `provider_unavailable`. Теперь подтверждено исчерпание output limit
без текста ответа. Расход всего лимита на reasoning остаётся гипотезой;
usage-доказательства нет. Это не подтверждённый timeout. Receipt:
`cloudru-compaction-provider-failure-20261026-v4.json`.

В v5 Task `2418a00a-0535-40a3-a45d-e8b530a61494` закреплён временный snapshot:
context window 20480 и structured output `reasoning=off`,
`compatibility=chat_template`. Общие настройки сразу восстановлены до прежних
128000/auto (revision 15). Операторские логи подтвердили два `context.compacted`:

| UTC | Working tokens до → после | Capacity | Заменённые sequence |
|---|---|---|---|
| 13:01:14.817412 | 15417 → 2373 | 9617 | 1–4 |
| 13:04:22.646762 | 15864 → 2353 | 9617 | 1–7 |

В предоставленном интервале `context.compaction.failed` нет: генерация и commit
двух summary в этом snapshot доказаны. Но проверка сохранения задания не прошла:
после двух точных follow-up по 39300 bytes и двух ответов на вопросы появился
третий `core_ask_owner`, повторяющий первый вопрос с новым call ID. Дубликатов
истории нет, финальный ответ пуст. Driver остановил сценарий и отменил только
собственную Task; состояние CANCELED, logout 200. Содержимое summary недоступно,
поэтому место потери задания пока не установлено. Успешное сжатие не закрывает
gate сохранения фактов/задания после повторного сжатия. Receipts:
`cloudru-batch-compaction-questions-20261026-v5-reconciliation.json`,
`cloudru-compaction-events-20261026-v5.json`.

Экспорт spans после rollout получает 403 (`11:43–11:47 UTC`). Startup-логи
подтверждают `OTEL_ENDPOINT_API_KEY=set` в Pod; доступность его значения
в интерфейсе не проверена. Отдельный GET к точному `/v1/traces` без настоящих
credentials, с проверкой TLS и без redirect, установил поведение gateway:

| Переданный формат | Ответ |
|---|---|
| Без ключа | 401, `WWW-Authenticate: X-API-KEY`, `Server: Evolution API Gateway` |
| `Authorization: Bearer` с заведомо неверным ключом | 403, `Invalid authorization header format` |
| `Authorization: Api-Key` с тем же неверным ключом | 400, `invalid api key secret` |
| `X-API-KEY` с тем же неверным ключом | 400, `invalid api key secret` |

Это подтверждает несовместимость Bearer с конкретным Cloud gateway и разбор
форматов Api-Key/X-API-KEY. Для платформенного alias `OTEL_ENDPOINT_API_KEY`
нужен формат `Authorization: Api-Key`. Настоящий injected key в этой проверке
не использовался: его права и успешный экспорт spans после исправления ещё
не доказаны. Receipt: `cloudru-otel-auth-format-20261026.json`.

Эти результаты относятся к опубликованному пакету и конкретным live snapshot.
Они не подтверждают новые незавершённые правки, native/browser release gates,
реальные CPU/min-replica настройки deployment или весь production-профиль.

### Новый локальный пакет после live-диагностики

Локально проверены валидация настроек database pool (`962c5ad`), пересечение
capabilities с поддерживаемыми platform features и gate `core_response_files`,
а также совместимая загрузка прежних checkpoints. Старые digest проверяются
в историческом режиме, но execution/dispatch всегда использует строгие текущие
gates; новые snapshots сохраняют версию фильтра. Платформенный alias
`OTEL_ENDPOINT_API_KEY` теперь выбирает `Authorization: Api-Key`; явный
`OTEL_API_KEY` сохраняет приоритет и прежний Bearer для обычного OTLP/Phoenix.

Итоговый локальный `uv run python -m unittest discover -s tests -v` завершён
`13:48:01 UTC`: 2424 tests за 448,156 секунды, exit 0, 16 skips; полный запуск
занял 451,452 секунды. Receipt подтверждает `source_unchanged=true` для четырёх
изменённых implementation-файлов. `uv run ruff check core_agent tests` и
`git diff --check` также завершились с exit 0. Независимый review не выявил
замечаний; отдельная inline-проверка подтвердила историческую проверку digest
при строгом dispatch. Receipt: `cloudru-final-local-batch-20261026.json`.

На момент этого прогона использованы существующие tests. Позднее пользователь
явно разрешил изменение frozen tests/spec; в следующем локальном пакете
добавлены regression checks и обновлены нормативные документы. Skips не закрывают
обязательные native/browser gates.
Сборка и публикация candidate по итогам дополнительной регрессии описаны ниже;
rollout этого пакета в Cloud пока не подтверждён.
Успешный экспорт с настоящим injected key и сохранение задания после повторного
сжатия по-прежнему не доказаны; локальный suite не закрывает эти live gates.

### Локальная диагностика на собственной PostgreSQL в Docker

По явному указанию пользователя диагностика продолжена локально. Cloud.ru
Kubernetes и platform PostgreSQL недоступны оператору напрямую; доступ к ним
для этой проверки не требуется. Локальная тестовая schema изолирована и удаляется
fixture после проверки; credentials production не используются для базы.

Новый PostgreSQL regression воспроизводит два вопроса владельцу, два больших
follow-up, два pressure compaction и два открытия нового app/pool. Он проверяет
точные пары вызов/успешный ответ `GO1` и `GO2`, сохранённый kernel и его budget,
original provenance, неизменный transcript и следующий HTTP model payload.
Валидные scripted summaries сохраняются корректно; это доказательство persistence
и assembly, отдельно от качества живой модели.

Локальная `.env` предоставляет DeepSeek endpoint, отличный от deployed Qwen.
На захваченных PostgreSQL model inputs живая модель сначала выдала summary со
всеми семью пустыми секциями. Старый валидатор принял её. Новый валидатор
отклоняет такую summary как `invalid_summary`, оставляя исходный context;
отдельные пустые секции допустимы. Prompt суммаризации теперь явно сохраняет
подтверждённые ответы владельца и call IDs как завершённые действия.

После этого обе summary содержали ответы и актуальные значения, но ordinary
model call всё равно повторял первый вопрос. Контролируемое сравнение одной
и той же summary при temperature=0 подтвердило: raw JSON приводит к повтору,
пояснение о сохранённом ходе задачи — к правильному итоговому JSON без tools.
Такое пояснение добавляется к model-facing summary текущей задачи; previous-task
history его не получает и не возобновляет старые цели, summary не выдаёт permissions.
Fixed framing учитывается в base budget, persisted summary JSON не меняется.
Изменение base kernel для этого исправления не требуется.

При проверке actual HTTP body также выявлен отсутствующий ordinary OpenAI
output cap. Общий adapter теперь передаёт единственный корректный cap для
OpenAI/Anthropic и streaming; extra-body может только уменьшить ceiling.
Отдельный summary output limit сохраняется. Regression checks сначала дали
ожидаемые ошибки на прежнем коде, затем прошли с исправлениями.

Диагностические receipts в ignored `.enterprise-work/`:
`cloudru-regression-prefixed-20261026.receipt.json`,
`cloudru-local-pg-compaction-wire-framed-20261026.json`,
`cloudru-local-summary-framing-ab-20261026.json`,
`cloudru-local-summary-kernel-guidance-20261026.json`.
Первый model replay не передавал output cap; v2/v3 использовали окно 20000
вместо 20480 в PostgreSQL fixture. Они сохраняются как диагностические попытки,
а не доказательство точного production request. Финальная проверка использует
обновлённый capture с framing, окно 20480 и явные HTTP caps. Локальный узкий tool catalog
оставляет pinned prompt чуть ниже целевой доли context, поэтому его summary
budget значительно меньше Cloud-сценария; `summary_too_large` при этом не
обходится увеличением лимита или усечением данных.

Review выявил отдельный прежний gap: cold recovery и динамический rebuild
инструкций используют текущий KernelCompiler, а не snapshot исходных kernel
segments. Повторное открытие той же версии приложения не доказывает неизменность
kernel через upgrade. Этот gap не исправлен и не закрыт данным локальным proof;
текущий пакет не меняет base kernel.

Финальный локальный model replay с actual framed PostgreSQL inputs прошёл:
две summary с первой попытки (364 и 424 оценочных токена), затем финальный
ответ без tools и повторного вопроса. В обеих summary сохранились подтверждённые
ответы с original source IDs; итоговый JSON содержит ROUTE_C, artifact-retention,
427.50 EUR и PENDING/NOT RELEASED. Ответ также содержит поясняющий текст —
strict JSON-only формат этим запуском не подтверждён. Всего три HTTP200 вызова,
явные output caps 3996/3486/4096, TLS verification и запрет redirects.
Receipt: `cloudru-local-pg-summary-model-replay-framed-20261026.json`.
Это локальная DeepSeek-проверка, а не завершённый Cloud.ru Qwen gate.

### Полная регрессия и опубликованный candidate нового пакета

Первый полный `uv run python -m unittest discover -s tests -v` завершился
26 октября в `15:23:24 UTC`: 2442 tests за 516,423 секунды, exit 1,
2 errors и 16 skips; полный запуск занял 519,695 секунды.
Receipt `cloudru-batch3-full-local-20261026.json` подтверждает
`source_unchanged=true`. Обе ошибки относятся к прежним узким fixture budgets:

- `test_public_reply_tools_free_context_fits_without_spending_summary_turn`:
  новое резервирование framing заняло ещё 47 оценочных токенов; окно
  пересчитано с 870 до 917.
- `test_runtime_compacts_twice_and_keeps_prompt_and_full_transcript`:
  расширенные summary instructions добавили 161 оценочный токен; окно
  пересчитано с 2000 до 2161.

Изменены только эти два fixture windows и поясняющие комментарии; assertions
о двух compactions, полном transcript и ответе без дополнительного summary
turn сохранены. Runtime validation и budget guards не ослаблены. Обе целевые
проверки прошли за 0,011 секунды. Повторный полный suite v2 завершился
в `15:47:23 UTC`: 2442 tests за 421,077 секунды, exit 0, 16 skips;
полный запуск занял 424,752 секунды. Receipt
`cloudru-batch3-full-local-v2-20261026.json` подтверждает
`source_unchanged=true` и `changed_sources=[]`. Ordinary suite прошёл;
пропущенные проверки не объявляются пройденными release gates.

В `15:49:21 UTC` опубликован linux/amd64 candidate
`blockchain.cr.cloud.ru/core-agent-gn:cloudru-validation-batch3-20261026`.
Registry-проверка подтвердила этот tag и `blockchain.cr.cloud.ru/core-agent-gn:latest`
с одинаковым digest
`sha256:fa86321ac341570c4f8664e26c7b697658851cda74fb106a30df7dbe0201bd96`.
Image smoke завершился с exit 0 в `15:21:29 UTC`: HTTP caps OpenAI/Anthropic,
framing текущей summary и all-empty summary rejection проверены. SHA-256
шести implementation-файлов (`config.py`, `database.py`, `observability.py`,
`context.py`, `model.py`, `runtime.py`) внутри образа совпали с рабочей копией.
Receipts: `cloudru-batch3-image-build-20261026.json`,
`cloudru-batch3-image-smoke-20261026.json` и
`cloudru-batch3-image-published-20261026.json` в ignored `.enterprise-work/`.

Rollout этого candidate в Cloud подтверждён пользователем; digest работающего
образа через публичный API независимо не проверен. Native/browser и target Cloud
gates, успешный экспорт с настоящим injected telemetry key и native compaction
evidence остаются отдельными обязательствами. Текущий Cloud Qwen replay описан ниже.
Локальный DeepSeek replay выше не доказывает Cloud behavior или strict JSON-only
ответ. Стабильность kernel snapshots через upgrade также остаётся открытым gap.

### Cloud Qwen replay после rollout batch3

Публичные `/health/live`, `/health/ready` и owner Agent Card вернули HTTP 200;
Card объявляет A2A protocol version `1.0`. Пользователь подтвердил rollout
`latest`, но публичный API не позволяет независимо сверить deployed image digest.

Задача `dd8d992c-793f-402a-bd86-fb8769128936` выполнялась 26 октября
`17:57:24–18:01:39 UTC` на `Qwen/Qwen3.5-397B-A17B` и достигла
`TASK_STATE_COMPLETED`. Её snapshot использовал `context_window=20480`,
`reasoning=off`, `compatibility=chat_template`. Подтверждены ровно два вопроса
владельцу и оба ответа, без повторного вопроса. Исходный prompt и оба follow-up
по 39300 bytes сохранились в owner history с точным совпадением bytes/SHA-256.
Итог сохранил последний маршрут `ROUTE_C_CMP_8f5e065ed5af`,
`artifact-CMP_8f5e065ed5af`, `427.50 EUR` и ожидание approval.

Exact final-format checks не прошли: вместо JSON-only получены Markdown fence
и поясняющий текст; `release` содержит `PENDING` вместо требуемого
`PENDING; NOT RELEASED`. Reconciliation отделяет сохранённый pending-смысл
от проваленного exact-literal check. Owner settings восстановлены:
`context_window=128000`, structured output `auto/auto`; logout — HTTP 200.

Public owner API не показывает summary events, поэтому сохранение данных и
завершение задачи не доказывают число native compactions. Operator evidence
для `context.compacted`/`context.compaction.failed` уже запрошено. Положительное
доказательство OTLP-доставки после rollout также отсутствует: локального
collector credential и публичного exporter-status API нет, health 200 не
проверяет exporter. Native/browser gates и полная main parity остаются открыты.

Receipts в ignored `.enterprise-work/`:
`cloudru-batch-compaction-questions-20261026-v6.json` и
`cloudru-batch-compaction-questions-20261026-v6-reconciliation.json`.

Короткий контроль без давления на context выполнился на той же Qwen
в `18:08:00–18:09:04 UTC`: Task `360f6b10-b6f6-4bd7-bb3d-b42fd38fbbcf`
завершена за 64,5 секунды. Строгий `json.loads` всего финального текста
подтвердил один точный JSON-объект, включая `release: "PENDING; NOT RELEASED"`,
без fences и пояснений. Выполнен один `core_response_begin`, два model turns;
settings не менялись, root admission не повторялся, logout — HTTP 200.
Receipt: `cloudru-short-json-control-20261026.json`.
Это исключает обязательное добавление prose самим `core_response_begin`,
но один короткий контроль не отделяет влияние compaction от сложности
длинного контекста или поведения модели. Причина нарушения формата в v6
не установлена; его exact checks не объявляются пройденными.

### Локальные proofs и текущий batch4: 27 октября

| Проверка | Результат | Граница доказательства |
|---|---|---|
| PostgreSQL/S3/storage/recovery/cron | 149 tests за 114,354 s, exit 0: 146 прошли, 3 SGID skips на macOS | Собственные PostgreSQL/MinIO fixtures; обычные runtime fixtures используют ScriptedModel/test launcher |
| Те же три SGID-сценария в Linux | 3 tests за 2,210 s, exit 0, без skips | Existing amd64 image с read-only source/test overlay; не native security proof |
| PostgreSQL waits/leases/owner sessions | 25 tests за 9,444 s, exit 0, без skips | Реальные локальные database state, leases, password rotation и session fencing |
| Landlock/seccomp в ARM64 VM | 40 tests за 50,889 s, exit 0, без skips; оба required flags | Linux 6.12.113, ABI 6, QEMU TCG, current Landlock sources и старый cached ARM64 rootfs; не exact candidate или Cloud host |
| Docker restart identity на ARM64 | 1 test за 3,063 s, exit 0, без skips | Реальный container restart и current mounted execution identity; не замена Cloud Pod |
| Production configuration | 14 tests за 0,186 s, exit 0, без skips | Config/schema validation, без доказательства настроек действующего deployment |
| Compose Bubblewrap composition root | 1 test за 23,648 s, exit 0, без skips, required sandbox flag | Реальные terminal/Python/background launcher; cached ARM64 rootfs с current source overlay |

Storage-набор покрывает 174 разные проверки после отдельного закрытия SGID
skips; security/config/restart-набор — ещё 56 tests. Это targeted proofs,
не полный suite актуального пакета. Проверены quiesced pg_dump/pg_restore со
ссылочными S3 objects и encrypted peer, runtime role grants, сохранность originals
и результатов, GC, limits, cleanup, waits, leases и owner sessions. Fault cases
S3 outage/timeout используют инъекцию ошибки adapter/SDK: физическое отключение
S3/сети, disk-full/OOM и поведение Cloud deployment этим не проверены.
Первая SGID-попытка ошиблась адресацией fixture; loopback relay исправил её без
ослабления S3 validation. Её failed receipt сохранён.

Прямой required Landlock gate на Docker LinuxKit 6.12.54 не прошёл: 34 tests,
1 failure и 1 error, exit 1. Filesystem probe вернул 77 для
`ENOSYS`/`EOPNOTSUPP`, обязательный preflight отказал. Успешная VM не закрывает
native matrix. Дополнительный amd64 batch3 VM fixture остановлен через 15 минут
на скачивании Debian index, до tests (exit 137); новый amd64 proof не получен.
Dedicated browser/native Pod fixture отсутствует; native amd64/browser gates
не объявляются пройденными. Временные собственные VM/Compose fixtures удалены.

Receipts: `cloudru-parity-local-gates-20261027.json` и каталог
`cloudru-parity-storage-20261027/` (`storage-recovery-tests.json`,
`waits-leases-sessions.json`, `linux-sgid.json`, `linux-sgid-attempt1.json`)
в ignored `.enterprise-work/`. Точные commands, logs и source scopes находятся
в receipts; source overlay не считается проверкой состава поставляемого image.

Первый полный ordinary suite batch4 завершился в `10:35:19 UTC`: 2451 tests
за 461,393 s, exit 1, 2 failures, 2 errors и 16 skips; `source_unchanged=true`.
Failures — два provider subcases прежнего ожидания исключения для unknown tool
в `test_transfer_features`; errors — два private compile snapshots без нового
kernel pin в `test_public_output_runtime`. Fixtures приведены к новому контракту.
Повторный suite v2, начатый в `10:38:44 UTC`, выполнил 2451 tests за 445,589 s:
exit 1, 1 error, 16 skips, `source_unchanged=true`. Прежние четыре отказа отсутствуют.
`RuntimeTests.test_root_cancel_recursively_cancels_child_and_grandchild_workflows`
получил `TimeoutError` в `scheduler.wait(child, timeout=1)`. Fixture теперь
освобождает блокирующую model stub после сохранения cancel intent grandchild,
не ожидая возврата всего recursive cancel. Проверки отмены descendants и
запрета позднего результата, а также таймауты assertions сохранены.
Изменение не меняет production cancellation behavior.

Последовательный полный suite v3 выполнен в `11:46:30–11:55:28 UTC`:
2451 tests за 534,738 s, exit 0, 16 skips; полный запуск занял 538,223 s.
`source_unchanged=true`, `changed_sources=[]`; независимый review блокеров
не обнаружил. Предыдущие failures сохранены как история проверки fixtures,
а skips не объявляются пройденными native/browser gates.
Receipts: `cloudru-batch4-full-local-20261027.json` и
`cloudru-batch4-full-local-v2-20261027.json`, итоговый
`cloudru-batch4-full-local-v3-20261027.json`.

Локальный `core-agent-gn:cloudru-validation-batch4-20261027` имеет image ID
`sha256:14f3a3d4d62d4511fec5b6aaf2d9c98db3c400984ccd234981ef9fa609457a1e`.
Это linux/amd64 delta от опубликованного batch3 `sha256:fa86321ac341570c4f8664e26c7b697658851cda74fb106a30df7dbe0201bd96`:
все base layers сохранены, один COPY layer заменяет только `kernel.py`,
`model.py`, `runtime.py`. Их source/image hashes совпали, actual-import smoke
без сети прошёл. Полный Dockerfile rebuild не удался из-за недоступного registry
mirror; daemon settings не менялись. Recipe и evidence:
`cloudru-batch4-delta-20261027.Dockerfile`, `cloudru-batch4-delta-build-20261027.json`,
`cloudru-batch4-delta-smoke-20261027.py/.json`.

После успешного suite и review image опубликован: registry readback
в `12:00:27 UTC` подтвердил для
`blockchain.cr.cloud.ru/core-agent-gn:cloudru-validation-batch4-20261027`
и `blockchain.cr.cloud.ru/core-agent-gn:latest` один immutable manifest digest
`sha256:14f3a3d4d62d4511fec5b6aaf2d9c98db3c400984ccd234981ef9fa609457a1e`.
`latest` обновлён только после успешной публикации и проверки version tag;
затем оба tags прочитаны из registry повторно. Receipt:
`cloudru-batch4-image-published-20261027.json` с push logs и registry readbacks.
Публикация не доказывает rollout batch4, native/platform gates или readiness
целевого Cloud deployment. SGR image не собирался и не публиковался в этом этапе.

### Уточнение уже выполненных Cloud-проверок

Receipt `cloudru-chat-isolation-20261026.json` подтверждает четыре проверки:
исходный файл проверен до пробы, доступ к соседнему workspace отклонён,
свой файл совпал с ожидаемыми bytes, исходный соседний файл не изменён.
Это реальная ограниченная Cloud isolation proof, но не полный сценарий двух
одновременно работающих root/child задач или replacement/overlap.

В `cloudru-cron-idle-20261026.json` первоначальный top-level check был false.
Последующая reconciliation доказала `automatic_state_before_first_read=true`:
задача начала работу в `06:41:04 UTC`, первое чтение выполнено в `06:42:30 UTC`.
Затем требовалось подтверждение начального guardrail; после exact approval
задача стала `COMPLETED` с точным marker. Это автоматический admission за
три минуты тишины, без запроса, запускающего выполнение; unattended completion,
длительный idle и гарантированный CPU вне HTTP не доказаны.

27 октября выполнен дополнительный Qwen control с теми же смысловыми
инструкциями, двумя вопросами и двумя пакетами данных, но без инертного padding.
Task `8fe86ca6-615e-4047-a55c-f7bf4dea2454` выполнялась
`10:34:31–10:42:27 UTC` и завершилась: два вопроса, оба ответа, точные originals,
актуальные route/artifact/amount, восстановленные settings и logout HTTP 200.
Inputs уменьшены до 1192/364/342 bytes. Учёт показывает 4 model turns и
3 tool calls — минимум для двух вопросов, `core_response_begin` и final;
отсутствие учтённого summary следует из этого учёта, native events недоступны.
Ответ вновь содержал fenced JSON и `release="PENDING"`: JSON-only и exact
literal checks не прошли. Validator ожидал `PENDING; NOT RELEASED`, но prompt
говорил `release must be PENDING; NOT RELEASED if approval is absent.` — без
кавычек вокруг literal и без JSON-примера. Эта естественно-язычная формулировка
допускает неоднозначное прочтение; возвращённое `PENDING` сохраняет pending-смысл.
Отрицательный exact check сохранён, однако он не доказывает потерю release-факта
или дефект модели/суммаризации. Compaction не является необходимым условием
наблюдаемого несоответствия валидатору; его конкретная причина не установлена.
Receipts:
`cloudru-no-pressure-questions-20261027.json` и
`cloudru-no-pressure-questions-20261027-reconciliation.json`.

Ordinary suite, review, сверка source/image hashes и публикация batch4 завершены.
Без оператора доступна дальнейшая сверка матрицы с существующими receipts.
Dedicated native/browser CI требует подходящего отдельного fixture. Фактические
Cloud CPU/min-replica настройки, Pod replacement/overlap, ingress revocation,
target backup/restore и native compaction events требуют платформенного evidence;
локальные tests не закрывают эти пункты. Telemetry исключена из текущих работ.

## 6. Когда цель считается достигнутой

Все основные возможности текущего main проходят согласованную матрицу на
Cloud.ru; новые Cloud storage/auth/sandbox границы имеют обязательные proofs.
Данные и ожидающие задачи переживают обновление в пределах заявленного
контракта, активные side effects не повторяются молча. Есть проверенный
production-профиль, воспроизводимое обновление/восстановление, фиксированный
образ и понятный список отличий от Kubernetes/main.
