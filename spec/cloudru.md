# Профиль Cloud.ru Evolution AI Agents

Статус: accepted target; обязательства выпуска и доказательства учитываются в
[v1](releases/v1.md) и [трассируемости](implementation-status.md).

## CLD-01. Явный профиль и конфигурация

`CORE_AGENT_AUTH_MODE` принимает ровно `keycloak` и `cloudru`, default —
`keycloak`. Прежний профиль сохраняет Keycloak authentication, caller isolation,
browser PKCE и постоянный POSIX workspace. Выбор `cloudru` явный; отсутствие
Keycloak, ошибка авторизации или недоступность sandbox не включают его автоматически.

Cloud.ru profile MUST задавать `CORE_AGENT_TENANT_ID`, отдельный secret
`CORE_AGENT_OWNER_PASSWORD` и `AGENT_URL` — канонический публичный HTTPS origin
без userinfo, query, fragment или пути кроме `/`. Только в этом профиле
`URL_AGENT` является fallback при отсутствии `AGENT_URL`. Trusted origin не выводится из входящих
`Host`, forwarding headers или Message metadata. Приложение доступно только
через изолированный ingress платформы, проверяющий доступ; прямой публичный
доступ к listener запрещён. Browser UI и API должны быть доступны через
проверенный платформенный browser access без помещения API-ключа в JS или URL.

PostgreSQL остаётся единственным production state store. Используются существующие
`SESSION_STORAGE_TYPE=postgres`, `TASK_STORAGE_TYPE=postgres` и
`SESSION_POSTGRES_*`; порядок DSN — `SESSION_DATABASE_URL`, составной DSN при
непустом `SESSION_POSTGRES_HOST`, `TASK_POSTGRES_URL`, `DATABASE_URL`.
При включённой памяти требуется `MEMORY_STORAGE_TYPE=postgres`; без отдельного
`MEMORY_POSTGRES_HOST` она использует общий пул. Production auto-migration
запрещена; operator запускает миграцию отдельно с `DATABASE_MIGRATION_URL`
либо `DATABASE_URL`, так как migration CLI не собирает `SESSION_POSTGRES_*`.
Нет fallback к in-memory/SQLite при ошибке БД или несовместимой схеме.

MCP connections и credentials берутся только из сохранённых owner UI settings.
Автоматически переданные платформой `MCP_URL`, `MCP_HEADERS_JSON` и credentials
MUST NOT создавать подключения, попадать в initial settings или заменять их.
Capability ceiling и policy checks для сохранённых MCP tools сохраняются.

## CLD-02. Владельцы и cookie-сессии

Владельцы используют один deployment password длиной не менее 16 символов;
это общий owner authority без
персональной identity каждого человека. Backend проверяет пароль и ограничивает
попытки входа. Пароль не попадает в browser storage, settings, Tasks, logs,
telemetry, модель, MCP, A2A или sandbox environment.

Успешный login создаёт криптографически случайную непрозрачную сессию. БД хранит
только digest token, tenant, срок и проверяемую связь с текущим password secret.
Срок фиксирован: 8 часов с выдачи, без продления при чтении. Cookie называется
`__Host-core-agent-owner`, имеет `Secure`, `HttpOnly`, `Path=/`, `SameSite=Lax`,
не имеет `Domain`. Logout отзывает сессию в БД и удаляет cookie; истёкшая,
отозванная или неизвестная сессия не даёт owner authority. Смена пароля отзывает
прежние сессии; deployment MUST согласованно обновить secret на всех экземплярах.
Рестарт и второй экземпляр используют общую БД, а не process-local sessions.

Login, logout и каждый изменяющий cookie-authenticated owner request требуют
ровно одного точного `Origin`, совпадающего с `AGENT_URL`. Отсутствующий, `null` или иной
origin отклоняется до мутации, включая соседний поддомен. SameSite не заменяет
эту проверку. Ответы login/bootstrap/session имеют `Cache-Control: no-store`;
токен не возвращается в JSON. UI отправляет same-origin cookies и после logout
очищает private state без автоматического повторного входа.

Публичный `GET /ui/config` возвращает ровно `{auth_mode: "cloudru",
external_a2a_url: "<origin>/"}`. `POST /api/auth/login` принимает
только JSON `{password}`, body до 4096 bytes: успех — 200 `{status: "ok"}`
и cookie с `Max-Age=28800`, неверный пароль — 401. Общий для tenant DB limiter
разрешает не более 10 попыток за 300 секунд, затем возвращает 429. Все корректно
сформированные попытки учитываются; успех, restart и смена пароля не сбрасывают
счётчик. `POST /api/auth/logout` возвращает 200 `{status: "ok"}`. Owner-only
`GET /api/identity` сохраняет прежние поля и добавляет `auth_mode: "cloudru"`.
Keycloak bootstrap/identity shape не меняется. Database password version
отклоняет login/session checks старого экземпляра после зарегистрированной
ротации, не позволяя ему возродить прежнюю сессию. Уже активированная и затем
заменённая password version сохраняется как retired и не может быть выбрана
повторно. Запуск replica с retired password завершается `CONFIG_INVALID` до
изменения действующей версии или её сессий; новая ротация использует новый secret.

Owner cookie открывает только owner A2A/API routes. External endpoint MUST NOT
получать owner authority из cookie, password, произвольного bearer или claims.
Owner-wide чтение/продолжение external Task сохраняет исходный owner её чата.
Секреты и cookies никогда не пересылаются исходящим MCP/A2A peers.

## CLD-03. Общий внешний scope

Канонический внешний A2A URL в Cloud.ru — `<origin>/`: `POST /` обслуживает
JSON-RPC, а HTTP+JSON использует существующие точные SDK routes от корня,
включая `/message:send`, `/message:stream` и `/tasks/...`. Agent Card доступна
по `/.well-known/agent-card.json` и `/.well-known/agent.json`; оба пути
объявляют trusted configured origin, без влияния `Host` или forwarding headers.
Основной binding/protocol contract остаётся [A2A `1.0`](a2a-protocol.md).
Произвольные пути и SDK tenant alias не становятся внешними входами.

Только внешний JSON-RPC от корня и его `/a2a/external/` alias дополнительно
принимают исторические методы A2A `0.3` через compatibility adapter официального
SDK. Они допускают отсутствие `A2A-Version` либо явную `0.3`; неверная явно
заданная версия отклоняется. Методы `1.0`, HTTP+JSON, owner вход и Keycloak
сохраняют прежний wire contract и version negotiation. Adapter преобразует
legacy запрос до общего handler и его ответ/ошибки/SSE обратно в `0.3` только
для этого запроса. Нет отдельного runtime, Task store или lifecycle; нормализованный
Message проходит те же admission/dedup/context, scope, size limits, file validation
и policy checks. Смена версии или входа не повторяет исполнение принятого Message.

Discovery внешнего входа разделяет версии без смешивания полей:

- `/.well-known/agent-card.json` сохраняет чистую карточку `1.0` и прежние
  `supportedInterfaces`; legacy interfaces в неё не добавляются.
- `/.well-known/agent.json` без заголовка версии либо с `A2A-Version: 0.3`
  возвращает чистую SDK projection `0.3`, объявляющую только поддержанный
  legacy JSON-RPC binding. С `A2A-Version: 1.0` возвращается та же карточка
  `1.0`, что на canonical path. Другие явно заданные версии отклоняются.
- Эти правила действуют и под external alias. Query parameters не выбирают
  версию; trusted origin, auth schemes и актуальная фильтрация skills сохраняются.
  Owner discovery и Keycloak карточки остаются прежними.

Совместимость с историческим URL меняется явно: клиент `1.0`, читающий
`agent.json` без version header, MUST перейти на `agent-card.json` либо передавать
`A2A-Version: 1.0`. Новые поля не добавляются в ответы/ошибки/SSE `1.0`, старые
поля не удаляются; legacy projection не меняет persisted state и не требует
миграции Tasks или контекстов.

Прежний `/a2a/external/` остаётся совместимым alias без redirect, с теми же
handlers, stores, lifespan, admission и external principal. Повтор одного
Message через другой вход не создаёт новую Task; изменённый Message сохраняет
прежний конфликт. Root A2A всегда получает external authority, в том числе
при переданном owner cookie; запрет ListTasks действует на обоих входах.
Owner A2A остаётся на `/a2a/owner/`, owner API, UI и probes сохраняют свои пути
и access checks. В Keycloak profile корневые A2A routes остаются закрытыми.

Только в Cloud.ru `GET /` и `HEAD /` выполняют тот же readiness predicate,
что `/health/ready`: healthy sandbox и доступная БД с совместимой schema дают
200, отказ — 503. GET возвращает тот же JSON status, HEAD — без body.
Это не A2A-вызов и не требует owner session; `POST /` остаётся JSON-RPC.
`/health/live` сохраняет отдельную liveness-проверку.

Cloud.ru proxy не предоставляет приложению проверенную identity peer-а.
Принятое решение — один стабильный external actor на deployment/company.
Он создаётся сервером, не зависит от credential string, IP, тела запроса,
UUID, `contextId`, forwarding headers или owner cookie. Отдельный app peer
secret, собственный API-key registry и автоматический provisioning не вводятся.

Каждому peer вручную выдаётся отдельный статический Cloud.ru service-account
API key; выдача и отзыв выполняются в Cloud console. Это позволяет отозвать
вход на платформенном proxy, но не разделяет peers внутри общего external
scope. Owner UI показывает A2A URL и эти инструкции без формы Keycloak provisioning
и без загрузки/сохранения platform keys. Отзыв ключа не отменяет принятую Task.

Любой допущенный платформой external peer может обращаться к известной external
Task/context этого scope: читать публичный результат, подписываться, продолжать
и отменять задачу в пределах её lifecycle. UUID не является аутентификацией;
конфиденциальность между этими peers не обещается. External Task listing и
enumeration полностью запрещены независимо от фильтров, cursor и binding.
Запрет проверяется до чтения списка и не возвращает IDs/count/metadata.
Владелец сохраняет свой общий список чатов и задач.

Owner Tasks, owner-private вопросы/ответы, HITL, settings, private guardrail
материалы и другие tenant scopes внешнему peer недоступны. Выполняемый моделью
код по-прежнему видит только workspace текущего чата; общий external principal
не открывает соседние каталоги, память или secret store в sandbox.
Дедупликация `(tenant, actor, messageId)` общая для внешних peers: одинаковый
запрос возвращает прежнюю Task, изменённый payload даёт существующий конфликт.
Разные platform keys не создают разные dedup namespaces.

## CLD-04. Хранилище и этап проверки durability

Полный Cloud.ru release требует PostgreSQL для state/manifests/receipts, S3
через SDK для immutable bytes и ограниченный RAM/POSIX cache для `/workspace`.
Прямая работа команд на S3 mount и FUSE не требуются. Первый trial сохраняет
временное POSIX-хранилище. Следующий этап включается явно через
`WORKSPACE_STORAGE_TYPE=s3` только в Cloud.ru development profile с PostgreSQL;
default `posix` сохраняет прежний режим. Ошибка S3 не включает временный fallback.
`WORKSPACE_S3_ENDPOINT_URL`, `WORKSPACE_S3_BUCKET`, `WORKSPACE_S3_REGION`,
`WORKSPACE_S3_ACCESS_KEY_ID`, `WORKSPACE_S3_SECRET_ACCESS_KEY` задают S3 connection;
в этом режиме для отсутствующих значений допускаются платформенные
`ARTIFACT_S3_*` с теми же suffix. Только для платформенного endpoint допускается
путь, точно равный `/<effective bucket>` или `/<effective bucket>/`: перед SDK
он удаляется, origin сохраняется; остальные paths, params, credentials, query и
fragment не становятся допустимыми. Явный `WORKSPACE_S3_ENDPOINT_URL` требует
origin. При fallback access key и заданном `ARTIFACT_S3_TENANT_ID` подпись
использует `tenant_id:key_id`; уже составленный key сохраняется только при
совпадении tenant, конфликт или malformed credential отклоняется без его значения
в ошибке. Явный `WORKSPACE_S3_ACCESS_KEY_ID` передаётся без добавления platform
tenant. Это S3 account, он не меняет trusted tenant namespace приложения.
`WORKSPACE_S3_SESSION_TOKEN` опционален,
`WORKSPACE_S3_PREFIX` по умолчанию `core-agent`. Настройки не публикуют ключи
в tools, browser, logs или sandbox; tenant namespace выводится из trusted
deployment scope. Подпись запросов выполняет поддерживаемый S3 SDK.
`WORKSPACE_MAX_BYTES` (default 100000000), `WORKSPACE_MAX_FILES` (default 10000)
и `WORKSPACE_S3_MAX_INFLIGHT` (default 4) задают конечные положительные лимиты.
Максимумы — 1000000000 bytes, 10000 entries и 16 одновременных S3 operations.
Сумма восстановленных деревьев одного процесса ограничена двумя per-tree
лимитами bytes/entries; исчерпание даёт явную ошибку до restore/checkpoint,
без скрытого удаления файлов. Это cache budget, а не замена process/cgroup
ограничений живых команд. Projection hydration сериализована с конечным
таймаутом и ограничена четырьмя `MAX_RESPONSE_SIZE` bytes и 256 references.
`DURABLE_STORAGE_ROOT` в этом режиме является локальным cache, а не источником
постоянных bytes. Смена bucket/prefix/tenant не является автоматической миграцией.

Target durability включает весь пользовательский проект: обычные и скрытые
файлы, `.git`, каталоги/permissions, attachments, A2A-файлы, промежуточные
результаты, изменения и удаления. Нет implicit ignores по имени или расширению.
Image skills/resources не копируются ради snapshot; пользовательская копия
сохраняется как обычный файл. `/tmp`, process state, supervisor secrets и файлы
вне workspace в этот snapshot не входят. Quarantine/results/offload тоже требуют
сохранения bytes до подтверждённого приёма/публикации и прежних access barriers.

Snapshot MUST принимать и восстанавливать `setgid` у каталогов, в том числе
унаследованный при обычном `mkdir` от родительского каталога платформы. Этот бит
задаёт наследование группы; он не делает каталог исполняемым с другой identity.
`setuid` у любой записи и `setgid` у файлов или symlinks MUST отклоняться при
capture и validation manifest до restore. Исходное дерево не нормализуется
через `chmod`; scope, nofollow и ограничения sandbox metadata не меняются.
UID/GID не входят в snapshot и остаются определёнными deployment environment.
Restore не вызывает `chmod`, если mode уже совпадает; после необходимого `chmod`
проверяет фактический mode. Тихое снятие бита ядром даёт `WORKSPACE_UNAVAILABLE`
до публикации восстановленного дерева, а не успешное восстановление с потерей прав.
Формат manifest v1 и DB schema сохраняются; прежние manifests читаются без
миграции. Старые readers, запрещающие directory `setgid`, отклоняют новые
manifests с этим битом: откат к ним после такого checkpoint не поддерживается.

Пустой чат/дерево не создаёт S3 archive/blob; удаление последнего файла сохраняет
DB revision без пустого архива. Неизменный checkpoint переиспользует manifest,
неизменные bytes не загружаются повторно. Полный snapshot фиксируется на безопасных
границах действий до объявления результата durable; изменение живым процессом
нельзя выдавать за согласованный checkpoint. Неизвестная команда не replay-ится.

Receipt запуска фоновой команды подтверждает запуск, а не snapshot живого
дерева. Он сохраняется без snapshot; завершение фонового процесса и terminal
cleanup проходят общий checkpoint barrier. Вложенный Python broker не снимает
snapshot, пока родительский Python ещё может писать. Обычные lifecycle/model
переходы не копируют дерево заново.

Детерминированный отказ terminal checkpoint (`WORKSPACE_SNAPSHOT_INVALID` или
`WORKSPACE_SNAPSHOT_LIMIT`) MUST NOT оставлять Task в бесконечном recovery.
После подтверждённой остановки execution family обычный fenced terminal commit
завершает её как `FAILED` с этим кодом вместо `COMPLETED`; уже выбранные
`CANCELLED`, `REJECTED` или `ABORTED` и их причины сохраняются. Непринятый
snapshot не публикуется, предыдущий manifest и ранее опубликованные результаты
остаются доступны. Принятый inbox получает прежнюю failure/cancel disposition;
модель и команды не вызываются повторно. Это правило применяется и к terminal
intent, сохранённому прежней версией приложения. Cleanup, lease/CAS и проверка
неизвестного исхода публикации остаются обязательными; временная ошибка S3
или изменяющееся дерево не считаются детерминированным отказом.

Commit требует immutable bytes с digest/size и затем fenced DB reference под
актуальными scope/lease/generation/revision, без S3 I/O под DB lock. Unknown PUT
сверяется для того же object; незакреплённые bytes не доступны caller/model.
Restore с пустой FS проверяет paths/types/permissions/digests, учитывает cleanup
revision/tombstones, не воскрешает удалённое и не открывает соседние чаты.
Finite RAM/tree/I/O limits и bounded GC не допускают тихих пропусков файлов,
удаления referenced bytes или ложного успешного checkpoint. Эти требования
проверяются локальными regression checks по CLD-AC-07–10; работа целевого
Cloud.ru S3 и восстановление после реальной замены Pod требуют отдельного прогона.
Storage events сообщают operation, outcome, размеры/количество и safe error code,
но не credentials, signed URLs, имена или содержимое пользовательских файлов.
Отказ проверки snapshot дополнительно сообщает конечный код причины: unsafe
path/link/mode, hardlink, неподдержанный тип записи или schema/metadata/parent
manifest. Raw exception и значения отвергнутых полей в этот код не включаются.

Миграция схемы добавляет manifests и staging/GC ledger, не меняя прежние
Task IDs, scopes или artifacts metadata. Она не переносит потерянные RAM bytes
и не импортирует POSIX blobs автоматически: новый trial использует отдельный
tenant/bucket prefix либо явно подготовленную миграцию. В S3-режиме входящие
оригиналы сохраняются до подтверждения приёма: bounded decode → immutable PUT →
atomic batch/Task/inbox reference в PostgreSQL. Сбой PUT не создаёт принятого
сообщения. Private quarantine остаётся недоступной модели и sandbox; review и
owner download читают original bytes с проверкой scope/digest/size, включая
после потери локальной FS. Сетевое чтение не удерживает DB transaction/locks.
Принятые и исключённые originals остаются referenced независимо от удаления
или изменения рабочей копии. Незавершённый upload очищается bounded GC.

После whole-batch allow рабочая копия публикуется вместе с workspace checkpoint
и DB receipt. До этого модель не получает пути/содержимое. Сбой
не оставляет незакреплённую копию доступной параллельным командам или tools
публикации файлов. При неизвестном исходе DB commit локальные starts, capture
и checkpoints блокируются до сверки receipt; подтверждённая публикация
сохраняет копию, неподтверждённая убирает её под общим локальным lock.
Повтор после сбоя использует тот же batch; уже опубликованная рабочая копия восстанавливается
только из текущего workspace manifest, без перезаписи изменений и воскрешения
удалений из оригинала. До публикации проверяются bytes/entries рабочего дерева
и общий resident budget, включая служебный manifest и родительские каталоги.
Исчерпание сохраняет pending delivery без частичной публикации.
Bounded original reads используют временные private каталоги, удаляемые после
чтения. Durable lease резервирует не более одного такого каталога на tenant;
после сбоя истёкшая 300-секундная lease позволяет bounded sweep убрать остаток
до следующего чтения. Пока прежняя lease жива, новое чтение получает retryable
storage error. Legacy batches без S3 references не мигрируют
автоматически и не объявляются durable. Target Cloud S3 и настоящая замена Pod
остаются отдельным release gate.

GC удаляет только unreferenced bytes после минимум 24 часов. PUT intent и
lease записываются до сетевой операции; manifest/artifact pin проверяется в
транзакции публикации. Каждая повторная incarnation digest имеет отдельный
object generation: запоздавший DELETE не удаляет новое содержимое. Bounded
проходы по 100 записей сохраняют tombstones для неизвестных поздних PUT.
Owner cleanup атомарно изменяет manifest и receipt. После потери RAM journal
сохранённый intent завершается только на пустом локальном cache и при совпадении
immutable digest/size/mode/mtime выбранного файла; изменившийся файл сохраняется.
Неизвестные/повреждённые intents сохраняют admission barrier.

## CLD-05. Исполнение и восстановление

По умолчанию `SANDBOX_BACKEND=bubblewrap`: существующие mount/PID/network
isolation, seccomp и resource limits обязательны для terminal, Python,
background и nested calls. Единственное исключение — явно выбранный
development backend по CLD-05-L ниже. Недоступность выбранного профиля
блокирует запуск; прямой subprocess, автоматическая смена backend или тихое
отключение инструментов не являются fallback.

S3 snapshot не доказывает остановку прежнего процесса. Смена Pod, потеря lease
или локальных handles не снимают execution cleanup barrier. Действуют прежние
правила exact stop proof/operator reconciliation. Неизвестный результат команды
или внешней мутации не воспроизводится ради восстановления файлов; безопасное
продолжение возможно только после проверки persisted outcome и cleanup.

Начальная конфигурация — один постоянно работающий экземпляр, CPU вне HTTP и
без scale-to-zero. Rolling overlap сохраняет требования PostgreSQL fencing,
shared owner sessions и одного writer workspace. A2A waits, cron, push и
recovery не зависят от открытого браузера или HTTP stream.

### CLD-05-L. Явный development backend Landlock

`SANDBOX_BACKEND` принимает только `bubblewrap` и `landlock`, default —
`bubblewrap`. `landlock` разрешён только при одновременных
`CORE_AGENT_ENVIRONMENT=development` и `CORE_AGENT_AUTH_MODE=cloudru`.
Неизвестный backend или другой profile/environment завершается
`CONFIG_INVALID` до запуска недоверенного кода. Отказ Bubblewrap никогда не
выбирает Landlock автоматически; production этим backend не разрешён.

Это ограниченное исключение из требований Bubblewrap и его виртуальных
mount/PID/IPC/network namespaces в остальных документах. Оно не обещает
такие namespaces: файловую границу задаёт Landlock, доступные syscalls —
seccomp, исходящую сеть — обязательный доверенный proxy. Tenant/owner scope,
guardrails/HITL, limits, durability, no-replay и execution cleanup barriers
сохраняются. По умолчанию и вне этого точного профиля прежние требования
Bubblewrap действуют полностью.

До serving обязательный реальный preflight MUST проверить Landlock ABI не
ниже 4, действующий файловый ruleset, `no_new_privs`, строгий seccomp filter,
user notification с `NEW_LISTENER` и фактическую передачу разрешённого socket
FD через `SECCOMP_IOCTL_NOTIF_ADDFD` ограниченному child. Каждый execution
устанавливает те же ограничения до запуска недоверенного кода. Номер ABI,
наличие API и отдельные diagnostic probes недостаточны. Недоступность любой
обязательной проверки или неподтверждённый cleanup прекращают startup с
`EXECUTION_ENVIRONMENT_UNAVAILABLE`; ослабление фильтра и прямой запуск запрещены.
Preflight проверяет выбранный backend; optional diagnostic probes MAY сообщать
доступность остальных механизмов, но не выбирают backend и не заменяют этот gate.
Проверка proxy использует реальную notification/FD передачу и отказ доступа
к запрещённому private HTTP destination; внешний интернет для preflight не
требуется. Перед ним `startup.sandbox_backend` сообщает только выбранный backend.

В этом backend workspace имеет настоящий scoped путь. Процесс получает его
как `HOME` и начальный `cwd`; относительный tool `cwd` и `/workspace/...`
остаются API-обозначениями одного чата и проходят прежнюю проверку границ.
Файлового alias `/workspace` внутри shell/Python нет; произвольные `argv`, shell
строки и Python source MUST NOT переписываться. Tool descriptions и выдаваемые
runtime пути файлов MUST соответствовать этой семантике. У каждого execution
свой `TMPDIR` вне workspace; общий `/tmp`, соседние чаты, server state, secrets,
service-account token и чужие broker sockets недоступны. Временные bytes
исключены из workspace snapshots и удаляются только после подтверждённой
остановки всего execution. Разрешаются только необходимые read-only runtime
файлы, свой workspace и свои временные файлы. Inherited descriptors закрываются
перед запуском, кроме явно разрешённых stdio и собственного tool broker transport.

Path-based `chmod`/`fchmodat` и операции timestamps, включая `utime`/`utimensat`,
MUST проходить доверенную seccomp notification mediation. Supervisor копирует
аргументы из памяти caller с конечными лимитами и сам выполняет разрешённое
изменение через закреплённые descriptors корней workspace/TMPDIR и безопасно
открытого файла. `SECCOMP_USER_NOTIF_FLAG_CONTINUE` запрещён: kernel не должен
повторно читать изменяемые caller pointers после проверки. Symlink, traversal,
смена каталога и ссылка на чужой FD не открывают файл за разрешёнными корнями.
Обычный `fchmod` своего уже открытого FD MAY использовать только mode bits
`0777`; setuid/setgid/sticky bits запрещены. Runtime binaries/libraries MUST
принадлежать root и быть недоступны для записи process UID. Эта ограниченная
metadata поддержка обеспечивает обычные Git init/clone/checkout; `chown`,
xattr, inotify и операции сигналов чужим процессам остаются запрещёнными.

Первый Landlock trial не поддерживает защищённые owner attachments. Новые
такие uploads MUST отклоняться с `CAPABILITY_DISABLED` в общем file admission
до принятия batch/сообщения, включая параллельно работающие background commands.
Публикация прежнего quarantined protected batch тоже отклоняется до записи
файлов в workspace; уже сохранённые bytes, history и owner file access остаются.
Это закрывает появление защищённых вложений после проверки, пока execution
уже работает. Перед новой командой непустой `attachments` дополнительно
даёт фиксированную безопасную ошибку `EXECUTION_ENVIRONMENT_UNAVAILABLE`.
Обычные созданные агентом файлы и прежние writable A2A файлы в `a2a/`
сохраняют свой контракт. Нельзя имитировать read-only вложения через `chmod`,
переписывание `open` или широкое разрешение записи в parent workspace.
Bubblewrap сохраняет прежнюю поддержку read-only attachments.

Исключение для явного S3-режима: новые owner uploads сохраняют закрытый
неизменяемый original по CLD-04, а после allow получают writable рабочую копию
в `inputs/<batch>/`. В A2A сохраняется `a2a/`. Это не read-only mount: агент
может менять/удалять копию, но не original. Default POSIX и старые batches
в `attachments/` по-прежнему запрещены Landlock; они не превращаются в mutable
копии автоматически. Snapshot/receipt сохраняют редактирование копий при recovery.

Сеть доступна только proxy-aware HTTP/HTTPS клиентам через доверенный
посредник. Seccomp запрещает прямое создание/подключение сетевых и Unix sockets,
UDP, raw sockets и обход через альтернативные syscalls; исключение — выданный
доверенным supervisor FD собственного Python tool broker. Разрешённый proxy
transport передаётся execution через проверенную user notification/FD injection.
Само наличие `HTTP_PROXY`/`HTTPS_PROXY` не является enforcement; удаление ENV,
`NO_PROXY`, прямой IP и собственный socket-код не обходят ограничение.
Каждый HTTP destination и HTTPS CONNECT проходит тот же `SandboxPolicy`, что
Bubblewrap: только разрешённые DNS servers, все DNS answers и фактический
адрес соединения проходят public/global и `SANDBOX_DENIED_CIDRS` проверки.
DNS rebinding, redirects, IPv4/IPv6 и IPv4-mapped IPv6 не открывают private,
loopback, link-local, metadata, cluster и другие запрещённые адреса.
Proxy не пересылает credentials приложения и имеет конечные request, connection,
I/O и time limits. Клиенты без поддержки proxy и произвольные TCP/UDP протоколы
не входят в этот профиль; direct network fallback запрещён.

Приложение MUST работать под non-root real/effective UID как PID 1 своего
выделенного container PID namespace; effective/permitted/inheritable/ambient
capabilities должны быть нулевыми.
Shared PID namespace, supervisor-обёртка как PID 1 и запуск без этих проверяемых
предпосылок отклоняются до serving. На каждый execution работает доверенный
subreaper с отдельной фиксированной process group. Недоверенный код и потомки
не могут выполнить `setsid`/`setpgid`, менять namespaces, воздействовать на
чужие процессы, ptrace/process memory или получать чужие descriptors.
Timeout/cancel/close и нормальный выход команды требуют остановки и reap всего
execution tree, включая осиротевшие процессы; до этого `sandbox_terminated`
не возвращается. Смерть trusted supervisor не оставляет execution работающим;
подтверждение остановки опирается на полный tree либо проверенную смерть
выделенного PID-1 lifetime контейнера. При неопределённом исходе сохраняются
`SIDE_EFFECT_UNKNOWN`, запрет нового execution и прежний reconciliation barrier.
Пропажа локального handle, lease или HTTP stream не является stop proof.

CLD-AC-14–17 требуют автоматических проверок и настоящего preflight плюс
terminal/Python/background smoke в целевом Cloud.ru Pod. Local/mock proof
не закрывает этот deployment gate и не даёт статус `implemented` автоматически.

## Первый диагностический запуск

Пользовательский первый trial включает Cloud auth, owner UI, platform PostgreSQL
и выбранный обязательный sandbox. Он использует `CORE_AGENT_ENVIRONMENT=development` и
`CORE_AGENT_AUTH_MODE=cloudru` с обязательной PostgreSQL, без дополнительного
trial switch, и MAY использовать временные RAM/POSIX каталоги до реализации
CLD-04. Production Cloud startup до реализации полного storage отклоняется. Это ограниченный nonproduction запуск: workspace,
upload/quarantine/result/offload bytes не имеют гарантии сохранности при замене
Pod, даже если metadata находится в PostgreSQL. Durable workflows/sessions в БД
не делают эти bytes постоянными. Файловые acceptance criteria и готовность
production S3 release этим запуском не подтверждаются. Существующие данные
не переносятся автоматически, trial не является разрешением потерять их.

В Cloud.ru trial автоматически переданные `ARTIFACT_STORAGE_*`, `ARTIFACT_S3_*`,
`ARTIFACT_MONGODB_*` и `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS` игнорируются без
чтения их значений и создания backend/SDK. Они не включают S3, не импортируют
данные и не блокируют этот trial. Keycloak/legacy profiles сохраняют прежний
startup rejection retired named-artifact settings.

`CORE_AGENT_STARTUP_DIAGNOSTICS=true` включает конечные структурированные записи
стадий configuration, model_configuration, database/schema, sandbox, runtime,
routes и recovery внутри общего startup. `startup.storage` сообщает
`backend=temporary-posix`, `durable=false` в Cloud trial. `application_ready`
фиксирует успешную сборку ASGI application; он не обещает запущенный listener.
Фактическое открытие socket выполняет Uvicorn после сборки приложения и
проверяется по его сообщению и HTTP probes.
Каждая запись содержит только allowlisted stage/status, факты наличия
необходимой конфигурации без её значений, finite sandbox facts и безопасную конечную
причину ошибки. Полный ENV, secret/password/token/cookie, DSN, raw URL/query,
body, private paths и traceback не выводятся. По умолчанию диагностика выключена;
существующие безопасные fatal startup сообщения сохраняются. Включённые logs
не отключают проверок и не заменяют sandbox preflight. Пакет первого trial
предназначен для самостоятельного запуска пользователем и передачи этих logs.

При включённой диагностике перед созданием штатного launcher и его preflight
выполняются отдельные проверки `startup.sandbox_probe`. Переданный тестовый
launcher не запускает эти проверки; при выключенной диагностике дополнительные
процессы не создаются. Фиксированный доверенный код в отдельных процессах
проверяет native `unshare`, создание user namespace, отображение UID/GID в root
этого namespace, затем mount/PID/IPC/network/UTS namespaces. Отдельно Bubblewrap
запускает только `/usr/bin/true`: с минимальной файловой системой, с `/proc`,
затем со всеми namespace и `/proc`, `/dev`, временным `/tmp`. Эти проверки
не исполняют код модели, не наследуют ENV приложения и не используют TUN.

Перед namespace-проверками отдельные доверенные процессы проверяют Landlock ABI,
установку файлового ruleset и seccomp-фильтра. Landlock должен сохранить чтение
`/dev/null` и запретить повторное открытие `/dev/zero` с `EACCES`; оба пути
проверяются до ограничения. Seccomp должен изменить результат прямого syscall
`getppid` с успешного на `EPERM`. Эти проверки не читают пользовательские файлы,
не создают файлы, не подключаются к сети и не ограничивают основной процесс.
Результат содержит только конечные status/reason/operation, errno и номер ABI,
если он доступен. Успех подтверждает только проверенное действие, а не готовый
execution backend или полноценную изоляцию файлов, процессов и сети.

Выполнение одной проверки ограничено двумя секундами; общий бюджет запуска
проверок — 10 секунд. Для подтверждённой остановки её процессов отдельно
отводится не более одной секунды. Stderr ограничен 4096 байтами и используется только для выбора
конечного reason-кода; raw stderr и тексты исключений MUST NOT попадать в logs.
Запись содержит фиксированные status/reason и состояние cleanup; operation,
errno и returncode добавляются, когда доступны. Отказы публикуются на уровне `CRITICAL`. Зависшие процессы
останавливаются с подтверждением завершения; неподтверждённая очистка блокирует
startup. Исчерпание бюджета не считается успешной проверкой.

`EPERM` сообщает об отказе операции, но не доказывает, что его вызвал AppArmor
или seccomp. Успешные отдельные проверки не подтверждают работоспособность полного
sandbox: штатный preflight остаётся обязательным и определяет возможность запуска.
Диагностика не меняет execution backend, сетевой профиль или security settings.

## CLD-06. Совместимость и выпуск

Профиль выбирается deployment-ом, не caller-ом; A2A payload/output contract
не меняется. Ограничение external listing и общий scope являются явными
profile semantics. Миграции БД для sessions/manifests additive и versioned;
их применяет operator до нового serving process. Переключение auth mode не
переназначает прежних владельцев: existing Keycloak external чаты требуют
явного проверяемого mapping либо остаются архивом прежнего deployment.

Перенос existing bytes выполняется с проверкой manifest/digest после остановки
новых admissions и согласованного backup БД, файлов и encryption key. Production переход
на RAM/S3 без проверки восстановления с пустой FS запрещён. После новых writes
откат требует совместимых schema/readers/storage references либо согласованного
restore; старый backup нельзя накладывать поверх новых данных.

Локальная реализация и автоматические проверки разрешены до доступа к целевому
Pod. Выпуск Cloud.ru MUST дополнительно пройти live проверки proxy/browser,
PostgreSQL grants/schema, S3 Put/Get/Delete/limits, idle workers/SIGTERM и
реальный sandbox preflight с terminal/Python/background smoke в целевом Pod.
Mock, другой кластер и успешный запуск reference-проекта не заменяют этот gate.
