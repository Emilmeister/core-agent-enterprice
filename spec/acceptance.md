# Критерии готовности целевого продукта

## Cloud.ru: PostgreSQL, S3 и общий внешний scope

CLD-AC-01–12 и CLD-AC-18–32 обязательны для production профиля [Cloud.ru](cloudru.md).
Первый диагностический trial ограничен CLD-AC-01–06/13/18–19; явный
Landlock opt-in в development/production дополнительно требует CLD-AC-14–17/32. Он не закрывает
S3 durability или live platform gates. Эти критерии уточняют
Keycloak-specific ENT-AC-01/03, browser/provisioning ENT-AC-72/85 и способ
хранения FILE-01; CLD-05-L отдельно ограничивает execution только выбранного
Landlock profile. CLD-AC-31 обязателен и для development с включённой памятью.
Остальные security, policy и lifecycle guarantees сохраняются.

| ID | Дано / действие | Наблюдаемый результат |
|---|---|---|
| CLD-AC-01 | Default/unknown mode, неполный Cloud config, platform PostgreSQL ENV и автоматически переданный `MCP_URL` | Default сохраняет Keycloak; unknown/incomplete fail closed; Cloud использует общий PostgreSQL pool и только persisted UI MCP settings; отсутствующая БД не включает in-memory fallback |
| CLD-AC-02 | Верный/неверный password, oversized body, более 10 попыток за 300 секунд, restart, второй экземпляр | Login выдаёт только непрозрачный Secure/HttpOnly/host-only cookie на 8 часов; в БД только digest; 401/429 не создают сессии; shared limiter не сбрасывается успехом, restart или password rotation |
| CLD-AC-03 | Logout, expiry, password rotation с живым/перезапущенным старым replica, login/logout/mutation с отсутствующим, повторённым или чужим Origin | Отозванная сессия и старый replica не получают owner authority; startup с retired password отклонён без отзыва новых сессий; CSRF отклоняется до side effect; UI очищает private state и не входит автоматически; Keycloak flow не изменён |
| CLD-AC-04 | Два platform keys обращаются к известной external Task, повторяют один messageId; пытаются ListTasks на обеих bindings | Известный external Task доступен обоим по lifecycle; общий dedup возвращает ту же Task либо conflict; внешний List полностью запрещён до выборки, без IDs/count; owner enumeration доступен |
| CLD-AC-05 | External caller с owner cookie/поддельными headers обращается к owner Task, settings, HITL/private material или чужому tenant | Authority остаётся external, private данные и мутации недоступны; исполняемый код не читает соседний workspace; UUID и credential string не выбирают scope |
| CLD-AC-06 | Owner открывает «Доступ к агенту»; один Cloud key отозван в console | UI показывает trusted A2A URL и ручные инструкции отдельного статического key на peer, не вызывает Keycloak provisioning и не хранит keys; real proxy закрывает новые запросы отозванного key; принятая Task не отменяется |
| CLD-AC-07 | Новый чат без файлов; затем обычный/скрытый файл, каталог/permissions, изменение, удаление, повтор неизменного checkpoint; обычный `mkdir` под Linux-каталогом с `setgid` | Пустой чат не создаёт S3 archive/blob; полное дерево без implicit ignores сохраняется; directory `setgid` принимается и сохраняется после restore без изменения исходного дерева; неизменные bytes/manifest переиспользуются; удаление последнего файла не создаёт пустого архива |
| CLD-AC-08 | Заменён Pod, RAM FS полностью пуста после сохранения проекта, attachments/quarantine, typed result и offload | До чтения/исполнения восстанавливаются committed paths/permissions и проверенные bytes; доступ/guardrail barriers сохранены; cleanup/tombstones не воскрешают удалённые файлы; image skills не копируются без пользовательского действия |
| CLD-AC-09 | Сбой до/после S3 PUT и DB commit, неизвестный PUT, stale writer, corrupt blob, traversal/symlink/special file, `setuid` у любой записи или `setgid` у не-каталога в дереве/manifest либо лимит RAM/tree | Нет ложного durable receipt, пропуска файла, доступа за chat root или commit старого worker; опасные mode bits отклоняются до restore; S3 I/O не удерживает DB lock; неизвестный PUT сверяется; GC не удаляет referenced/live staged bytes |
| CLD-AC-10 | Pod потерян во время команды; имеется snapshot, но нет доказательства остановки/результата; другая Task ждёт A2A/HITL | Команда не replay-ится; прежний cleanup/reconciliation barrier сохраняется; committed файлы и разрешённые durable waits восстанавливаются без повторных side effects |
| CLD-AC-11 | Реальный target Pod: preflight и terminal/Python/background/nested execution, idle worker, SIGTERM/rolling overlap | Обязательный текущий sandbox работает; недоступность закрывает запуск без fallback; cron/push/recovery работают без HTTP, fencing исключает второго writer |
| CLD-AC-12 | Реальный Cloud proxy/browser, platform DB migration/grants и S3; перенос старого deployment с проверкой пустой FS | UI/assets/cookies/SSE/A2A и bytes доступны через реальный endpoint, секреты не встроены в UI; migration/version/backup подтверждены; прежние owner IDs не переназначаются автоматически; непроверенный target gate блокирует выпуск |
| CLD-AC-13 | Первый trial `development` + `cloudru` с обязательной PostgreSQL, временным workspace и включённой/выключенной `CORE_AGENT_STARTUP_DIAGNOSTICS`: успешный старт либо отказ на config/DB/schema/sandbox; доступные/недоступные Landlock ABI, файловый ruleset и seccomp filter; отказ syscall/Bubblewrap, timeout, oversized stderr и неподтверждённый cleanup диагностической проверки | Конечные типизированные stage/status/reason logs показывают достигнутую стадию без ENV/secrets/DSN/raw URL/body/private paths/traceback/raw stderr; opt-in `startup.sandbox_probe` сначала запускает `landlock_abi`, `landlock_filesystem`, `seccomp_filter` в доверенных Python-процессах с `-I -S` без наследования ENV, затем native/Bubblewrap проверки; Landlock сообщает ABI и после успешного baseline обоих путей сохраняет чтение `/dev/null`, запрещая `/dev/zero` с `EACCES`; seccomp меняет успешный прямой syscall `getppid` на `EPERM` явным правилом; общий лимит исполнения каждой проверки 2 секунды, бюджет запуска набора 10 секунд, отдельный cleanup до 1 секунды и stderr не более 4096 байт; failures видимы на CRITICAL, cleanup failure блокирует startup; выключенная диагностика и переданный test launcher не запускают probes; успешные probes не заменяют preflight выбранного backend и не меняют backend/сетевой профиль; trial не обещает сохранность bytes после замены Pod, production Cloud startup до S3 отклонён; retired artifact ENV не подключают backend и не блокируют trial; application_ready подтверждает только ASGI app |
| CLD-AC-14 | Default/unknown `SANDBOX_BACKEND`; Landlock вне Cloud development/production; ABI < 4, root UID, ненулевые capabilities, процесс не PID 1; недоступный ruleset/seccomp/NEW_LISTENER/ADDFD или cleanup failure | Default остаётся Bubblewrap; несовместимая configuration даёт `CONFIG_INVALID`; обязательный реальный preflight выбранного backend отказывает до serving с `EXECUTION_ENVIRONMENT_UNAVAILABLE`, без запуска кода модели и fallback; успех включает действующее ограничение файлов/syscalls и получение injected socket FD child, а не только проверку наличия API |
| CLD-AC-15 | Landlock terminal/Python с relative, native absolute и `/workspace/...` tool cwd; shell использует HOME/TMPDIR; попытки доступа к соседнему чату, host `/tmp`, server secrets, `/proc` чужого процесса, symlink escape; chmod/timestamps, Git init/clone, чужой FD и directory swap; новые protected uploads во время background, прежний quarantined batch и непустой attachments | Настоящий cwd/HOME относится к своему scoped workspace, `/workspace` остаётся только cwd API alias; argv/source не переписываются; TMPDIR отдельный вне snapshots и удаляется после tree cleanup; недопустимые bytes и descriptors недоступны; chmod/timestamps выполняет trusted broker после bounded копирования arguments через pinned root/file FD без CONTINUE, Git работает только внутри writable roots; fchmod собственного FD допускает лишь mode bits 0777 при root-owned immutable runtime, chown/xattr/inotify и отправка сигналов закрыты; новые protected owner uploads отклоняются с CAPABILITY_DISABLED до shared admission, прежняя quarantine publication — до workspace writes, даже при background execution; непустой attachments дополнительно отклоняет команды с EXECUTION_ENVIRONMENT_UNAVAILABLE; bytes/history/owner access, generated files и writable a2a/ сохраняются; Bubblewrap поддержка вложений сохраняется |
| CLD-AC-16 | HTTP и HTTPS CONNECT через proxy; direct IP/socket, UDP/Unix, unset proxy/NO_PROXY, запрещённый или mixed DNS answer, rebinding, redirect, IPv4/IPv6 и metadata/cluster targets; чужой broker | Разрешённый proxy-aware HTTP/HTTPS проходит только через доверенный FD injection и общий SandboxPolicy; все DNS answers и адрес реального connection проверяются; прямой сетевой обход и чужой broker закрыты; доступен только собственный bounded Python tools broker; finite proxy limits и отсутствие server credentials сохраняются; ошибки не включают direct network fallback |
| CLD-AC-17 | Native Linux Landlock terminal/Python/background/nested calls: fork/orphan, setsid/setpgid, signal/ptrace/process-fd escape, timeout/cancel/normal exit, смерть supervisor и PID-1 приложения | Trusted subreaper и фиксированный PGID удерживают descendants; escape/воздействие на соседний execution запрещены; `sandbox_terminated` появляется только после подтверждённого полного cleanup; потеря supervisor не оставляет tree; неизвестный outcome сохраняет SIDE_EFFECT_UNKNOWN/reconciliation/no-replay; target Cloud Pod проходит тот же preflight и smoke, local/mock результат не закрывает deployment gate |
| CLD-AC-18 | Cloud caller читает обе root Agent Cards, вызывает JSON-RPC и HTTP+JSON от корня, повторяет Message через `/a2a/external/`, передаёт owner cookie и поддельные forwarding headers; читает GET/HEAD `/` при исправном и unhealthy sandbox, отказе БД/schema; те же root A2A запросы в Keycloak | Root cards и `/ui/config` используют configured origin; alias работает без redirect через общие admission/stores и возвращает ту же Task либо прежний conflict, без повторного исполнения; owner cookie не повышает external authority, owner-private Task/API закрыты и listing запрещён на обоих входах/bindings; GET/HEAD `/` совпадают с `/health/ready` по 200/503, HEAD без body, POST остаётся A2A; owner/API/UI и `/health/live` сохраняют контракт, неизвестные/tenant alias paths и Keycloak root A2A закрыты |
| CLD-AC-19 | Cloud external root/alias: JSON-RPC 0.3 Send/Get/Cancel/Stream и эквивалентный 1.0 Message, version headers, обе карточки с query/поддельными headers/owner cookie; те же legacy вызовы на owner, Keycloak и HTTP+JSON; malformed/oversized input и FileParts | Только external JSON-RPC принимает legacy wire format через официальный SDK; Task/context/admission/dedup общие, нормальные file/size/policy/access barriers сохранены, owner-private данные закрыты; методы/ответы/errors/SSE/files 1.0 не меняются; canonical card чистая 1.0, historical card без header или с 0.3 чистая 0.3 и только JSONRPC, с 1.0 идентична canonical; неподдержанная explicit version отклоняется; owner/Keycloak и HTTP+JSON не получают 0.3 совместимость |

Дополнительные критерии S3 и поддерживаемого production profile:

| ID | Дано / действие | Наблюдаемый результат |
|---|---|---|
| CLD-AC-20 | Обычный запрос модели, retry, finalization и recovery проходят через серверные часы | Каждый физический основной запрос получает обновлённые дату/время/UTC offset в trusted instructions; budget учитывает их; compiled kernel и structured-output схемы не меняются |
| CLD-AC-21 | В `core_response_files` передан абсолютный sandbox path либо относительный путь внутри workspace | Абсолютный путь отклоняется с понятным указанием использовать относительный; пример в tool description использует `test.csv`; traversal/доступ вне scope не разрешаются |
| CLD-AC-22 | Workspace cleanup прерван после durable intent до/после локального unlink, новый процесс начинает с пустого cache | Сопоставление с immutable manifest атомарно завершает удаление и receipt, не восстанавливает выбранные bytes; изменённый файл сохраняется; неизвестный intent остаётся закрытым |
| CLD-AC-23 | Холодный S3 cache при owner/peer чтении, wait/approval/cancel/follow-up, scheduler admission и runtime transitions | Remote reads происходят после выхода из DB transaction; bounded hydration повторяет только rollback-safe DB операции, не model/tool/classifier calls или worker launch; повтор проверяет текущий доступ |
| CLD-AC-24 | Входящие root/follow-up файлы в S3-режиме; сбой PUT либо restart до решения guardrails | Приём подтверждается только после immutable PUT и атомарной DB ссылки с Task/inbox; при сбое нет принятого сообщения; scoped original доступен владельцу после потери cache, но недоступен модели до allow |
| CLD-AC-25 | Cloud S3/Landlock: owner разрешает вложение, агент редактирует/удаляет `inputs/<batch>/`, затем restart | Публикация копии и workspace checkpoint атомарны; правки и удаления сохраняются, original download из карточки входящего файла возвращает исходные bytes. History/receipt дают server-owned `download_path`; свежая history добавляет его и ранее принятым S3 batches без миграции. Чужие chat/Task/batch/index, неопубликованный и не-S3 batch не открывают original обычным маршрутом. Reject не публикует копию; private original остаётся на review endpoint, legacy `attachments/` не открывается для записи |
| CLD-AC-26 | S3 original временно недоступен после приёма; другой scope запрашивает original; GC проходит accepted/excluded batches | Доставка остаётся pending для recovery без model/tool replay; чужой scope отклоняется до GET; GC сохраняет referenced originals независимо от состояния рабочей копии |
| CLD-AC-27 | После rename рабочей копии checkpoint не записан либо исход DB commit неизвестен; параллельный child начинает команду или публикует файл | Незаписанная копия убирается до следующего доступа; неизвестный исход блокирует starts, file capture и checkpoints до сверки. Подтверждённая публикация сохраняется; retry не повторяет выполненные действия |
| CLD-AC-28 | Платформенный S3 endpoint содержит путь к бакету; key передан отдельно от storage tenant; explicit workspace overrides либо conflicting path/credential | SDK получает исходный origin и один bucket path, подпись использует один tenant prefix; explicit overrides сохранены; неизвестный путь, params и конфликт tenant отклонены; raw/composed keys не попадают в startup logs, app tenant scope не меняется |
| CLD-AC-29 | Terminal checkpoint отвергает дерево или его лимит; сохранённый terminal intent повторяется после recovery; имеется принятый inbox | После confirmed cleanup Task завершается с явной ошибкой без нового snapshot и повторных model/tool calls; прежние manifest/artifacts и failure/cancel disposition сохранены. Неизвестный cleanup и временные storage errors не обходятся; diagnostics содержат только конечную безопасную причину |
| CLD-AC-30 | Production Cloud с PostgreSQL, S3, валидным Fernet key, раздельными roots, отдельной миграцией и выбранным Landlock/Bubblewrap; по очереди отсутствуют auth/DB/S3/key, задан malformed key, POSIX, auto-migration или недоступен sandbox; прежние Keycloak и development trial | Полная configuration допускает сборку приложения только после обязательного preflight; каждый нарушенный gate отклоняет startup до serving без fallback и secret values в ошибке. Production `DATABASE_AUTO_MIGRATE=true` запрещён; app не получает DDL role. Keycloak сохраняет свой контракт; Cloud development POSIX trial остаётся явно ephemeral. Допуск конфигурации не означает прохождение live release gate |
| CLD-AC-31 | Cloud development/production с `CORE_AGENT_MEMORY=optional|required|disabled`; отсутствующий/default `in-memory`, неверный либо `postgres` backend; общий pool и отдельный memory DSN, недоступная БД | Включённая память требует PostgreSQL в общей точке создания backend, иначе `CONFIG_INVALID` до serving; нет in-memory fallback. `disabled` не создаёт backend; PostgreSQL использует общий pool либо проверенный отдельный DSN, сохранённый corpus доступен после restart без смены scope |
| CLD-AC-32 | Native Linux CI для каждой архитектуры candidate image с required Landlock/seccomp flags; enforcement или fixture недоступен | Существующие isolation/network/metadata/cleanup suites проходят с `CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1` и `CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1`; недоступность даёт failing gate, а не skip. Bubblewrap gates main сохраняются; local VM/native proof не закрывает real Cloud security/idle/SIGTERM/rolling gates |

Поставка может реализовать подмножество только через явный [release profile](releases/v1.md); реализованное поведение не может противоречить этим критериям.

## A2A и публичный контракт

- [ ] Core Agent публикует валидную Agent Card с protocol/binding versions, auth, media types, skills и capabilities.
- [ ] Карточка доступна и по `/.well-known/agent-card.json`, и по историческому `/.well-known/agent.json`; оба пути возвращают идентичный документ, кроме разделённого Cloud external discovery CLD-AC-19; query-параметры игнорируются.
- [ ] Каждая объявленная в карточке пара binding/version принимается соответствующим endpoint: `JSONRPC` и `HTTP+JSON` отвечают на `1.0`, карточка не содержит пар, которых нет, и не содержит полей карточки A2A 0.3 (`url`, `preferredTransport`, `protocolVersion` верхнего уровня); методы JSON-RPC 0.3 (`message/send` и др.) не принимаются, кроме явно ограниченного Cloud external adapter CLD-AC-19 с отдельной legacy карточкой.
- [ ] При заданном `AGENT_URL` карточка объявляет именно его, и никакой заголовок запроса это не меняет; `URL_AGENT` принимается как то же значение, а при обоих заданных выигрывает `AGENT_URL`.
- [ ] Без `AGENT_URL` карточка объявляет адрес из `X-Forwarded-Proto`/`X-Forwarded-Host`, иначе из схемы и `Host`; непригодное значение отбрасывается, а запрос карточки всё равно завершается успешно.
- [ ] Лишний член верхнего уровня JSON-RPC конверта не отклоняет запрос, не интерпретируется как session id и попадает в предупреждение с именами удалённых полей.
- [ ] Внешнее общение использует A2A Message/Task/Artifact; отдельная публичная task state machine отсутствует.
- [ ] Message Parts отображаются на `prompt`; собственное расширение протокола не требуется и не объявляется в Agent Card.
- [ ] Неподдерживаемая required extension/version возвращает стандартно отображаемую A2A ошибку до model turn.
- [ ] A2A `contextId` сохраняет session, но MCP/skills не наследуются в новую Task неявно.
- [ ] Blocking, `return_immediately`, polling, subscription/streaming и push видят одну durable Task history.
- [ ] Disconnect stream не отменяет Task; at-least-once push update дедуплицируется.
- [ ] Fresh subscription после process restart сначала отдаёт persisted Task, пассивно наблюдает durable updates и не resume-ит `WAITING_*`/`PAUSED`/`auth-required`; terminal update остаётся последним.
- [ ] Подписка на process-local active Task отдаёт ранее прочитанный persisted snapshot до live events и закрывается на первом terminal event без последующих artifact/status frames.
- [ ] Recovery terminalizes A2A Task и ставит push delivery одной транзакцией; ошибка enqueue откатывает обе записи, а повторная сверка создаёт ровно одну delivery. Восстановленный `failed`/`rejected` содержит безопасный stable error code и не раскрывает raw error.
- [ ] `LEASE_LOST` и graceful shutdown после durable admission не terminalize-ят A2A Task; shutdown до admission даёт безопасный `failed`, а не вечный `working`. Поздний `CancelTask` не превращает уже terminal workflow в `failed`, а отражает сохранённый итог локального producer или durable recovery. Cancel чужой/idle Task не оставляет process-local coordination entry.
- [ ] Follow-up Message с существующим non-terminal `taskId` возвращает ту же Task, durable переживает restart и попадает отдельным user turn перед следующим model call.
- [ ] Если `failed`/`canceled` выигрывает после принятого follow-up, terminal gate переносит Message в transcript с причиной `unprocessed_due_to_failure`/`unprocessed_due_to_cancel`, не вызывает модель или tool и не оставляет unread inbox.
- [ ] Duplicate `messageId` не доставляется дважды; concurrent Messages получают стабильный committed order; context/task mismatch и cross-tenant ID отклоняются.
- [ ] Уже начатый model/tool/side-effect call не прерывается; completion atomically проигрывает более раннему accepted Message, поэтому подтверждённый input не теряется.
- [ ] Follow-up не изменяет EffectiveConfig/MCP/skills/budgets.
- [ ] Internal states корректно отображаются только на стандартные A2A Task states.

## Конфигурация агента

- [ ] При shared PostgreSQL startup/recovery приложения компании B исполняет
  только работу B; root/background/remote/cancel/wait/cleanup/projection/push
  записи A не меняются и не используют модель либо credentials B. Более одного
  batch чужих rows не блокирует B; generic runtime без tenant scope сохраняет
  прежнюю platform-wide semantics, неизвестные rowless files не удаляются.
- [ ] PlatformConfig, AgentConfig и Task input разделены; Task не расширяет AgentConfig.
- [ ] AgentConfig может отключить memory, terminal, filesystem mutations, background tasks, delegation, отдельные built-in/MCP tools и skills.
- [ ] Disabled tool отсутствует в discovery/model context и stale call получает `CAPABILITY_DISABLED`.
- [ ] Memory modes `disabled`, `optional`, `required` корректно фильтруют/требуют встроенный memory backend.
- [ ] Неверные memory modes, включая boolean/null, отклоняются без отражения значения; явно включённая capability вне platform support не добавляет tools или policy. `response_files: false` исключает оба response-file имени и их policy, включая answer phase; пропущенные switches сохраняют прежние defaults.
- [ ] Root/delegated/background snapshots закрепляют `capability_filter_version=1`. Legacy digest без marker допускает recovery, включая старый skill digest, но модель и dispatch получают текущие ограничения; version/digest обновляются атомарно. Современный marker отклоняет старый feature digest; malformed/unknown marker отклоняется до workspace/discovery/cache.
- [ ] MCP tool filters применяются после discovery, но до model context; deny имеет приоритет.
- [ ] Admission ceiling и config/tool snapshot immutable внутри Task; текущая owner policy применяется к последующим calls, все использованные versions/digests сохраняются в audit.
- [ ] Agent Card не рекламирует capability, отключённую AgentConfig или текущим
  owner deny. После owner policy update оба A2A входа и оба well-known пути
  отражают deny/allow/HITL без restart; policy другой компании не влияет на Card.
- [ ] Присутствующая, но пустая deployment-переменная трактуется как отсутствующая
  и получает документированный default, кроме явно документированных allowlist:
  пустой `CORE_AGENT_ALLOWED_SKILLS` отключает навыки.
- [ ] Невалидная конфигурация не поднимает listener, завершает процесс ненулевым кодом и печатает ровно одну строку с именем настройки и стабильным кодом, без traceback и без значений secret-переменных.
- [ ] Пустые/whitespace pool settings сохраняют defaults 1/10/10; minimum 0 и minimum=maximum допустимы. Неразбираемые числа, неверные size bounds и nonfinite/nonpositive timeout отклоняются до создания PostgreSQL pool, без исходных значений и credentials.
- [ ] Неразбираемые числовые deployment values, `NaN`/infinite floats, неверный `LOG_LEVEL`, неизвестные `TASK_STORAGE_TYPE`/`A2A_CAPABILITIES` и нечисловые HTTP retry codes отклоняются с `CONFIG_INVALID`, именем настройки и без введённого значения; пустые значения сохраняют documented defaults, listener не запускается. Ошибка старта остаётся видимой при `LOG_LEVEL=CRITICAL/FATAL`.

## Kernel instructions

- [ ] KernelInstructions всегда присутствуют отдельно от AgentProfilePrompt и имеют version/digest в audit.
- [ ] Пустой AgentProfilePrompt не добавляет generic system instruction; фактический prompt присутствует только как отдельный user Message/context item.
- [ ] Base kernel выбирает tool по смыслу задачи, не требует tool без материальной пользы и не дублирует conditional capability/tool-description semantics.
- [ ] AgentProfilePrompt, user Message, skill или MCP output не могут изменить rules включённой capability; отключать optional capability может только config/policy.
- [ ] До каждого model turn контекст содержит имена и краткие descriptions всех и
  только skills из EffectiveConfig, но не содержит тела неактивированных
  `SKILL.md`, package paths или resources.
- [ ] Модель может выбрать skill по смыслу запроса без буквального имени и без
  slash-команды; `core_skill_activate` принимает enum effective names, закрепляет
  digest идемпотентно и добавляет полные инструкции только со следующего turn.
- [ ] `features.skills=false` или отсутствие platform feature дают пустой
  effective skill catalog и скрывают оба служебных tools независимо от allowlist.
- [ ] После запроса `core_skill_activate` оставшиеся calls того же assistant
  response не dispatch-ятся, получают `SKILL_ACTIVATION_BOUNDARY` и могут быть
  выбраны заново только следующим model turn с полным `SKILL.md`.
- [ ] Имена обоих служебных skill tools зарезервированы от MCP collision, а
  response/reasoning deltas удерживаются до классификации ответа и отбрасываются,
  если ответ запрашивает активацию.
- [ ] `core_skill_read_resource` существует только для активного skill, принимает
  enum `<skill>/<relative-path>`, читает bounded UTF-8 content из immutable
  snapshot и отклоняет неизвестный, бинарный, symlink, absolute или escaping path.
- [ ] Активация и чтение resource расходуют общий tool-call budget, проходят audit
  и OTel и возвращают recoverable structured failure; child получает эти
  служебные tools только из явно делегированного skills allowlist, без отдельных
  записей в `core_delegate.tools`.
- [ ] Runtime принимает при новом admission только закреплённые digest `SKILL.md`
  и полный manifest ресурсов и отклоняет подмену после activation. Legacy active
  skill продолжает сохранённые instructions без чтения live package, новых
  activations и ресурсов. После activation base context budget пересчитывается до
  следующего model call.
- [ ] Runtime enforcement отклоняет запрещённое действие, даже если model output просит обойти kernel instruction.
- [ ] Child получает ту же kernel version и не может ослабить parent/host policy.
- [ ] Provider-visible reasoning отсутствует в audit, memory и tool arguments; в A2A stream оно появляется только как помеченная `adk_thought` часть при `A2A_STREAMING_ENABLED=true` и вырезается из терминального кадра, а hidden/opaque reasoning не экспортируется никогда.
- [ ] Streaming-кадры несут reasoning, function_call и function_response с ADK-совместимыми метками, текст идёт кумулятивными снимками с `partial`, и поток заканчивается ровно одним `final:true`.

## Runtime и durability

- [ ] `THINKING_LEVEL` валидируется до первого turn, отображается в model invocation parameters и маппится в нативный OpenAI-compatible/Anthropic request без управления через RunRequest.
- [ ] Provider adapter отделяет visible reasoning от публичного ответа, сохраняет необходимый provider replay для следующего tool turn и экспортирует известный reasoning token usage.
- [ ] Capability negotiation отклоняет несовместимый model/adapter до первого turn.
- [ ] Model fallback не повторяет tool call и compacts context перед меньшим окном.
- [ ] Pause/passive wait освобождают model worker и продолжаются из checkpoint/notification.
- [ ] Lease не позволяет двум workers одновременно изменить Task.
- [ ] Lease регулярно продлевается во время model/tool/join работы дольше одного TTL; длинный joined child не завершает исправный parent с `LEASE_LOST`.
- [ ] Pending input и task notifications восстанавливаются с прежними IDs/revisions.
- [ ] Idempotent operation можно продолжить; неоднозначная мутация не повторяется.
- [ ] Hard limits включают parent и все child/background Tasks.
- [ ] Последний model turn заранее удержан внутри общего hard budget и вызывается с пустым tool catalog только для честного финального ответа; provider calls никогда не превышают limit.
- [ ] Charge каждого initial/retry provider attempt, local usage и pre-dispatch marker коммитятся атомарно; crash не создаёт бесплатный повтор, а неизвестный reserved finalizer не dispatch-ится второй раз.
- [ ] Charge model-issued tool request, local tool usage и pre-dispatch marker коммитятся атомарно; crash не списывает и не dispatch-ит один queued request повторно, rollback не расходится с root ledger.
- [ ] При исчерпанном tool budget ни один оставшийся call из assistant batch не dispatch-ится, каждый получает structured `BUDGET_EXCEEDED` с `tool_calls`, used/limit и указанием передать промежуточный результат выше.
- [ ] Исчерпание execution budget завершает Task как `completed` с `completion_reason=budget_exhausted`, `complete=false`, фактическим usage и текстом, разделяющим проверенное и незавершённое; Task не переходит в `failed` и модель не выдумывает недостающие results.
- [ ] Если финализирующая модель не вернула text или после retry недоступна, runtime публикует только детерминированное сообщение о неполноте без hidden reasoning/raw tool output; ambiguous mutation по-прежнему даёт `SIDE_EFFECT_UNKNOWN`.
- [ ] Follow-up, принятый во время reserved finalizer, durable попадает в transcript, не вызывает второй provider call сверх hard limit и явно помечается как необработанный в terminal partial result.
- [ ] Неиспользованный finalization reserve возвращается общему ledger атомарно с нормальным terminal transition; неудавшийся start child не оставляет ни task/workflow, ни занятую ёмкость.
- [ ] Scheduler handle, child workflow и его finalization reserve коммитятся одной транзакцией до запуска worker; ошибка admission не оставляет ни одной из трёх записей.
- [ ] Durable scheduler claim принадлежит одному worker, продлевается heartbeat-ом и не позволяет concurrent recovery запустить один contract дважды или stale worker записать terminal state.
- [ ] Live workflow lease исключает run из recovery даже при старте другой replica; совпадающий worker ID не заменяет живой token, а истёкшие workflow/scheduler tokens не renew-ятся и не terminalize-ят state; PostgreSQL expiry вычисляется по текущему server clock после ожидания row lock, независимо от clock skew replica и времени начала statement.
- [ ] Root recovery filter применяется до batch limit, поэтому любое число child workflow не блокирует восстановление root; graceful worker shutdown останавливает active и ожидающие continuation без terminal transition.
- [ ] Проигравшая workflow lease recovery-attempt завершается после одного claim без 50-миллисекундного polling; повтор допускается только следующим coordinator scan.
- [ ] Durable `cancel_requested` выигрывает у более позднего success/start failure и даёт `CANCELLED`; только неоднозначный `EXECUTING` outcome сохраняет `ABORTED`/`SIDE_EFFECT_UNKNOWN`.
- [ ] Workflow transition повторно проверяет lease token/expiry на финальном `core_runs` update после ожидания shared budget или других transaction locks; lock wait через TTL даёт `LEASE_LOST` и откатывает весь transition/charge.
- [ ] Mutating exception после committed intent, resume/recovery или cancel workflow в `EXECUTING` дают `SIDE_EFFECT_UNKNOWN` без redispatch и без ложного `CANCELLED`, включая scheduler notification.
- [ ] При restart `cancel_requested` recoverable Task согласует durable child state один раз, а non-recoverable mutating Task получает reconciliation error вместо ложного `canceled` outcome; cancel между scan и claim либо сразу после claim не обходит reconciliation и не стирает её ошибку.
- [ ] Cancelled child атомарно возвращает неиспользованный finalization reserve; начатый finalizer остаётся учтённым как provider call.
- [ ] Budget finalization ждёт owned Tasks только bounded grace; некооперативная Task остаётся durable/cancel-requested, а partial result возвращается и перечисляет её в `pending_tasks` без выдуманного outcome.
- [ ] Persisted result различает local `usage` и общий `shared_budget`; live и crash-recovered final Messages v2 (legacy — A2A Artifacts) публикуют их в одинаковой completion provenance metadata.

## Context и compaction

- [ ] Owner может задать/сбросить целочисленный размер окна без смены модели;
  invalid значения отвергаются, старые revisions используют deployment default,
  существующая задача и её children сохраняют admitted override. UI показывает
  источник значения; недоступный discovery не блокирует изменение только окна.
- [ ] Summary имеет отдельный выходной cap 32768 по умолчанию, ограниченный
  свободным входным окном; обычный model output reserve не меняется, проверка
  размера и provenance summary не ослабляется; provider extra_body не снимает cap.
- [ ] Обычные OpenAI/Anthropic HTTP requests, включая streaming, содержат
  выходной cap adapter; меньший явный provider cap сохраняется, больший ограничен.
  OpenAI поддерживает явный `max_completion_tokens` без второго cap-поля;
  неправильный тип и неположительный cap отвергаются до отправки.


- [ ] Два отказа обязательного сжатия и recovery checkpoint с attempts=2
  завершают задачу FAILED/CONTEXT_UNRECOVERABLE через cleanup barrier; повторное
  recovery не запускает модель/tools. Transcript и результаты сохранены;
  диагностика содержит конечную причину отказа без приватного содержимого,
  UI объясняет остановку. Неподтверждённый cleanup сохраняет terminal intent.

- [ ] Owner `/api/chats` показывает общий company список, включая чаты внешних
  callers, с bounded pagination и актуальным active root; external/dual-role
  получает 403, другая company не видит записи, чтение не вызывает модель.
- [ ] Base tokens считаются как system/kernel/profile + selected tool schemas + output reserve.
- [ ] Working occupancy учитывает только prompt/history/summaries/memory/notifications/artifact excerpts относительно оставшейся working capacity.
- [ ] System prompt и tool schemas не входят в 90%/10–15% threshold.
- [ ] Ниже 90% working occupancy compaction не запускается; при 90% и выше происходит до model call.
- [ ] После compaction working occupancy находится в диапазоне 10–15%.
- [ ] Prompt, policy, task contracts, active constraints, artifact refs и memory provenance остаются pinned.
- [ ] Повторные compactions сохраняют goal и immutable transcript mapping.
- [ ] Public UI bootstrap возвращает только configured issuer/public client ID,
  без server secret, introspection client и identity. Неверная/неполная browser
  configuration отклоняется при startup; bootstrap query не меняет issuer.
  Owner API/A2A остаются авторизованными, выключенный bootstrap недоступен.
- [ ] Owner registry CRUD имеет company scope, immutable name/revisions и CAS;
  secret omission сохраняет credential, null очищает, disabled revision не удаляет
  pinned старую. Header values отсутствуют в GET/errors/audit/model; ciphertext
  связан с tenant/peer/revision/header и не переносится между адресатами.
- [ ] Remote timeout/poll defaults86400/300 сохраняются старым settings PUT;
  операция закрепляет settings и peer revision. Deadline начинается до первого
  разрешённого Send; повтор remote wait не продлевает его и не принимает timeout
  override. Late result и cancel после timeout не открывают network/continuation.
- [ ] Новая root Task атомарно закрепляет previous root того же canonical чата;
  owner continuation external чата сохраняет execution owner. Metadata не меняет
  эту ссылку; duplicate и busy Task не вклиниваются в цепочку, а другая company
  или чат не становятся источником истории.
- [ ] Вторая и третья Task одного чата получают проверенные прежние решения и
  итог с original provenance; новая инструкция pinned, прежние цели historical.
  Recovery не дублирует импорт, старые tool calls/replay не отправляются provider.
  Foreign/child/nonterminal/missing источник отклонён. Отзыв материала исключает
  зависимые прошлые summary и final result; failed/partial outcome явно сохранён.
- [ ] Semantic summary сохраняет поздние исправления и различает факт, вывод,
  предположение и план; malformed/truncated/unknown-source ответ не заменяет
  исходный context. Из active provider context не исчезает половина tool-call/result пары.
- [ ] Summary со всеми семью пустыми секциями отвергается как `invalid_summary`;
  прежний context и transcript сохраняются. Отдельные пустые секции допустимы.
  Подтверждённый ответ владельца остаётся завершённым шагом после повторного
  сжатия; вопрос связан с точным ответом и call ID, а план вопроса не считается ответом.
- [ ] Compaction attempt заранее оплачена из root model budget; restart между
  attempt и commit не сбрасывает максимум двух попыток и не меняет source boundary.
- [ ] Ошибка interval compaction ниже pressure сохраняет прежний context;
  `LEASE_LOST` и storage ошибки по-прежнему прерывают исполнение.
- [ ] Материал, сначала допущенный и включённый в summary, после canonical
  отказа по тем же данным исключается вместе с зависящим summary из следующего
  model call и истории новой Task; полный private audit transcript остаётся.
- [ ] Непомещающиеся protected/pinned data дают `CONTEXT_UNRECOVERABLE`, не silent truncation.
- [ ] Крупный tool result целиком сохраняется в tenant-scoped artifact и immutable transcript, а active model context получает bounded JSON-ссылку, digest, размер и краткую выдержку вместо полного output.

## Память агента и file lifecycle

- [ ] Память является подсистемой Core Agent: `core_memory_*` являются built-ins, отдельный memory-процесс и MCP-роль `memory` отсутствуют.
- [ ] При `CORE_AGENT_MEMORY=disabled` memory tools/instructions отсутствуют, backend не создаётся и implicit fallback не выполняется; строка `"disabled"` не проходит gating как включённая capability.
- [ ] Markdown corpus является source of truth; BM25/vector/graph indexes полностью перестраиваются из него.
- [ ] Модель не имеет ни filesystem, ни SQL доступа к corpus и меняет его только `core_memory_*` tools.
- [ ] Namespace выводится из scope текущего run: модель передаёт только `user|session`, а `user_id`/`session_id` подставляет runtime.
- [ ] Модель передаёт заголовок и тело, а front matter формирует runtime; корректный YAML от модели не требуется.
- [ ] Перед create/update agent выполняет hybrid search и проверяет top candidates.
- [ ] Та же тема обновляет существующий документ; новый subject/scope создаёт новый.
- [ ] Create/update с 201+ body lines жёстко отклоняется без revision/index changes, truncation или automatic split.
- [ ] `MEMORY_FILE_TOO_LARGE` возвращает actual/max lines и рекомендацию разделить content на несколько документов.
- [ ] Ошибка memory tool возвращается модели как failed tool result и не завершает run: после `MEMORY_FILE_TOO_LARGE` модель вызывает `core_memory_split` в том же run.
- [ ] Отдельный `core_memory_split` принимает явный plan; каждый resulting документ также не превышает 200 lines.
- [ ] Split сохраняет stable IDs/aliases, ссылки и provenance без разрыва semantic block.
- [ ] Update использует expected revision; concurrent conflict не разрешается last-write-wins.
- [ ] Delete исключает content из Markdown, summaries, BM25, vectors, graph и caches.
- [ ] Перевод строки в `title`, `kind`, `status` или элементе `tags` отклоняется как `MEMORY_INVALID`, повторяющийся ключ front matter — тоже, а разобранные `id` и `namespace` сверяются с подставленными runtime-ом для каждого документа, включая дочерние в `split`; ни одна из этих строк не переписывает чужую заметку и не меняет namespace.
- [ ] Определение перевода строки совпадает с парсерным: U+2028, U+2029 и U+0085 отклоняются наравне с `\n`.
- [ ] Элемент `tags` с `,`, `[` или `]` отклоняется, потому что не переживает обратное чтение.
- [ ] Нечитаемая строка хранилища пропускается с warning и не делает недоступным остальной corpus пользователя.
- [ ] Атрибут degraded channels на search span совпадает с `degraded_channels` в tool result, включая деградацию, обнаруженную во время самого поиска.
- [ ] `update` и `split` сохраняют `tags` и `sources` исходного документа.

## Backend памяти и эмбеддинги

- [ ] `MEMORY_STORAGE_TYPE` принимает `in-memory` и `postgres`; доменная семантика лимита, revisions, retrieval и graph одинакова для обоих.
- [ ] Production и Cloud.ru в любом environment с включённой памятью без `MEMORY_STORAGE_TYPE=postgres` завершаются ошибкой конфигурации; `disabled` не создаёт backend.
- [ ] PostgreSQL backend публикует revision одной транзакцией, переживает рестарт и превращает конфликт номера repository revision в `MEMORY_CONFLICT`.
- [ ] Пул выбирается в порядке `MEMORY_POSTGRES_HOST` → общий пул агента → собственный по `DATABASE_URL`; durable память при `SESSION_STORAGE_TYPE=in-memory` работает, а отсутствие обеих переменных даёт ошибку конфигурации, называющую обе.
- [ ] Пул, открытый самой подсистемой, проходит миграцию или `verify_schema` до старта, а не падает на первом tool call.
- [ ] Conflict возвращает фактическую revision backend-а, подсистема перечитывает состояние до возврата ошибки, и предписанный retry успешно выполняется.
- [ ] `load()` не сочетает новый номер revision со старым corpus: гонка публикации приводит к конфликту, а не к потере чужой записи.
- [ ] Extraction выполняется только для изменённых документов и только моделью агента через `response_format` с полной JSON-схемой; отдельных `MEMORY_NER_*` переменных не существует.
- [ ] Отказ extraction не отменяет запись: документ публикуется с `entities IS NULL`, индексируется встроенным regex-экстрактором и остаётся находимым по BM25, а graph-канал сообщает число неизвлечённых документов без их имён.
- [ ] Сущность, отсутствующая в тексте документа дословно, отбрасывается; offsets вычисляет подсистема, а не модель.
- [ ] Ответ шлюза с кодом 4xx, кроме 408 и 429, выключает extraction до конца жизни процесса, и следующая запись не делает нового запроса к модели.
- [ ] Слой extraction включён только при заданных `LLM_MODEL`, `LLM_API_KEY` и адресе; `LLM_ENDPOINT` заменяет производный адрес, а `LLM_API_FORMAT=anthropic` выключает слой на старте.
- [ ] Сохранённые сущности переживают рестарт: загрузка corpus не выполняет ни одного запроса к модели.
- [ ] Многословная сущность находится по одному слову запроса; graph-канал сопоставляет токены, а не строку целиком.
- [ ] `content` в PostgreSQL хранит Markdown целиком, включая front matter; `kind`, `status`, `sources` и timestamps переживают рестарт без реконструкции.
- [ ] Отсутствие расширения `vector` логируется, понижает векторный канал до вычисления в процессе и не роняет ни миграцию, ни старт.
- [ ] Слой эмбеддингов включается только при заданных `EMBEDDING_MODEL`, `EMBEDDING_API_KEY` и базе; незаданная `EMBEDDING_API_BASE` берётся из `LLM_API_BASE`, а ключ не выводится ниоткуда.
- [ ] `OTEL_ENDPOINT` выводит только `/v1/traces`; metrics и logs уходят исключительно по явным per-signal endpoints, а `ENABLE_OTEL=false` отключает экспорт целиком.
- [ ] Имя сервиса в телеметрии берётся из `OTEL_PROJECT_NAME`, `OTEL_SERVICE_NAME` — синоним с меньшим приоритетом.
- [ ] На реальном OTLP HTTP receiver без иной transport/URL auth каждый настроенный signal получает Bearer для непустого `OTEL_API_KEY`, Api-Key для fallback `OTEL_ENDPOINT_API_KEY`, либо отсутствие auth header без ключей. Explicit key имеет приоритет, пустой explicit использует fallback; безопасная exporter metadata не содержит ключей.
- [ ] Любая ошибка embedding endpoint не роняет tool call; текст запроса и ключ не попадают в error, audit и telemetry.
- [ ] Embedding документа вычисляется один раз при публикации и сохраняется; search эмбеддит только запрос.

## Indexing, NER и graph

- [ ] Create/update/split/delete обновляет chunking, BM25, embeddings, NER, entity resolution и graph до atomic publication revision.
- [ ] Изменённый chunk удаляет stale mentions/relations; неизменённый сохраняет stable chunk ID/index data.
- [ ] Partial index failure не делает staging Markdown видимым обычному search.
- [ ] NER/relation edges имеют evidence line/chunk, confidence, extractor version и source revision.
- [ ] Low-confidence entity merge остаётся candidate; manual correction переживает full reindex.
- [ ] Изменение extractor/taxonomy строит новую revision в фоне и атомарно переключает её.

## Hybrid search и rerank

- [ ] Candidate generation независимо получает BM25, embedding и bounded graph candidates.
- [ ] Query NER/entity linking выполняется до graph traversal.
- [ ] Raw scores разных retrievers не складываются без calibration/RRF.
- [ ] Reranker получает text, heading, все component scores/ranks, graph paths, recency и provenance.
- [ ] Search result возвращает component/final scores, revision и provenance.
- [ ] Недоступный channel явно помечает degraded search; конфликтующие claims не скрываются ranking-ом.
- [ ] Candidate set, model/index/reranker versions позволяют воспроизвести retrieval decision.
- [ ] Поиск находит заметку на нелатинской письменности без настроенного слоя эмбеддингов: токенизация и извлечение сущностей не зависят от алфавита.

## Background Tasks

- [ ] `task.start` возвращает handle сразу, пока main agent продолжает независимую работу.
- [ ] Completion/failure/artifact/input notifications durable, versioned и at-least-once.
- [ ] Notification попадает в model context только на safe boundary и дедуплицируется.
- [ ] `task.wait` не создаёт busy polling и не удерживает model worker.
- [ ] Agent может ничего не делать до notification или timeout.
- [ ] Cancel/timeout завершает process tree; orphan policy обрабатывает Task после terminal parent.
- [ ] Parent не завершает зависимый итог, пока required Task pending.

## Сабагенты

- [ ] Сабагент создаётся как неблокирующая A2A Task.
- [ ] Child workflow, scheduler handle, task tools, notifications, logs и traces используют один стабильный task ID.
- [ ] Delegation contract содержит узкую instruction, exact tool/MCP/skill allowlists, memory policy и budget; child возвращает обычный text result без artifact tool/schema handoff.
- [ ] Delegation template и model-facing schema всегда требуют оба поля `budget.turns >= 1` и `budget.tool_calls >= 1`; runtime повторно отклоняет missing, zero и boolean до создания child Task.
- [ ] Parent делегирует coherent outcome и minimum sufficient capabilities, а не необязательные mechanical microsteps; runtime предоставляет exactly выбранный capability set.
- [ ] Model-visible delegation prompt/description применяют balanced decision rule: положительные triggers — materially useful parallel work, изоляция большого отделимого context или independently verifiable bounded deliverable; simple/serial/tightly coupled/duplicate/policy-bypass/generic-second-opinion работа явно остаётся у parent.
- [ ] Внутри objective/scope child самостоятельно выбирает strategy, sequencing и delegated tools; procedure фиксируется только для safety/correctness/reproducibility/policy.
- [ ] Child сообщает safe assumptions, но останавливается при выходе за scope, недостающей capability, новом side effect или существенном риске неверного result.
- [ ] Child не видит невыданные рабочие capabilities даже на discovery.
- [ ] Protocol-internal lifecycle/audit остаются enforced, но model-callable child tools равны пересечению EffectiveConfig и delegation allowlist.
- [ ] Child и main разделяют memory только при явной передаче того же memory scope и явного `core_memory_*` allowlist.
- [ ] Без переданных memory tools child работает без memory.
- [ ] Child memory write проходит service revision/index/NER pipeline и уведомляет parent.
- [ ] Parent воспринимает child text result как недоверенный input; child не общается наружу без capability.
- [ ] Budget-exhausted child возвращает через join/get/wait/notification одинаковый обычный text result с `complete=false`; parent передаёт проверенную часть выше, явно перечисляет unfinished scope и не повторяет выполненное.
- [ ] Depth/fan-out и child budgets ограничены общим parent budget.
- [ ] Cancel root рекурсивно сигнализирует child/grandchild; после ближайшей safe boundary новые model/tools не запускаются, scheduler и workflow имеют terminal `canceled`, а не `failed`.

## Local terminal sessions

- [ ] Main и каждый child получают разные TerminalSession IDs, PTY и process groups в границах постоянного workspace своего чата; завершение child сохраняет файлы чата.
- [ ] Terminal tool не может адресовать session/process другого agent или run.
- [ ] Typed `argv` используется по умолчанию; `cwd`, environment и output bounded и валидируются до запуска.
- [ ] Если child использует отдельную scratch-копию для изоляции изменений, она создаётся из проверенного immutable base snapshot, а merge использует base revision/conflict detection. Без scratch-копии пересекающиеся записи parent/child явно координируются.
- [ ] S3 mount хранит immutable snapshots/checkpoints/artifacts, но active command не выполняется непосредственно на нём.
- [ ] Cancel/timeout завершает owned process group, закрывает PTY и фиксирует cleanup outcome.
- [ ] Secret инжектируется только в environment разрешённого process и не попадает в checkpoint/telemetry/artifact.
- [ ] Runtime применяет обязательные Bubblewrap namespaces и egress, без fallback при недоступном профиле; факт проверки целевого кластера подтверждён отдельно.
- [ ] `core_python_exec` доступен в обоих runtime-профилях и управляется только built-in allowlist.
- [ ] Python process получает только bounded `tools.call`; каждый вложенный built-in/MCP вызов повторно проходит admission ceiling, schema, current owner policy/HITL, общий budget, owner/tenant, audit и OTel.
- [ ] Python exception/nonzero exit/timeout возвращается модели как failed tool result, не завершает родительскую Task и не повторяет неоднозначный side effect.
- [ ] Пакет, установленный командой из `core_terminal_exec`, импортируется в `core_python_exec` без правки `sys.path`; рабочий каталог при этом в `sys.path` не попадает.
- [ ] Образ v1 проходит реальную проверку основного набора CLI, включая Mike Farah `yq` и PDF-команды; краткое описание `core_terminal_exec` называет набор, явно не считает его allowlist и разрешает дополнительные workspace-инструменты только при допустимых policy и сети.

## Tools и MCP

- [ ] Tool arguments валидируются до policy и execution.
- [ ] Отказ валидации называет путь аргумента, ожидаемый тип и фактический; переданное значение в сообщение не попадает.
- [ ] Каноническое имя tool не содержит точки, совпадает с именем в каталоге модели и с именем в аргументах `core_delegate`; alias появляется только при коллизии или превышении длины.
- [ ] Имя MCP-тула разрешается в пару `(сервер, tool)` индексом; tool, в имени которого есть точка, вызывается на своём сервере.
- [ ] Schema `core_delegate` перечисляет enum-ом фактический каталог тулов родителя и его skills, а не свободные строки; отдельного аргумента `mcp` нет.
- [ ] Поля `core_delegate` имеют model-facing descriptions; отказ называет отклонённый tool/skill/budget dimension и перечисляет доступный соответствующий набор или limit.
- [ ] Один список `tools` несёт built-ins и MCP-тулы под именами каталога; runtime раскладывает их сам, а отказ называет отклонённое имя и перечисляет доступные.
- [ ] Аргумент со значением `null` обрабатывается как непереданный.
- [ ] Значение `CORE_AGENT_ALLOWED_BUILTIN_TOOLS`, записанное точками до переименования, продолжает называть тот же tool; неизвестное имя отклоняется с перечислением.
- [ ] Shell-синтаксис среди элементов `argv` даёт сообщение про отсутствующий shell, а не ошибку первой попавшейся утилиты.
- [ ] Built-in descriptions кратко и точно отражают фактические ownership/lifecycle ограничения, включая timeout snapshot и запрет task-start для Python/delegate/task/send_message tools; устаревшие config names `core_artifact_put/get` отклоняются.
- [ ] `core_agent_send_message` ретранслирует прогресс удалённого агента в поток корневой Task, возвращает durable handle; результат доставляется через wait, downstream использует только secret-header настройки адресата.
- [ ] Делегированный child использует выданные file/remote capabilities с тем же tenant/caller/chat scope и не получает credentials вызывающей стороны.
- [ ] Ни один входящий A2A Part не теряется молча: file Parts атомарно сохраняются по FILE-02; unsupported references отклоняются, произвольный caller URL не даёт SSRF или credential forwarding.
- [ ] MCP tools/resources/prompts/sampling/elicitation проходят local policy независимо от server metadata.
- [ ] MCP-сервер, согласовавший любую поддерживаемую ревизию протокола, подключается; отказ по версии называет предложенную и принимаемые.
- [ ] Выданный сервером `Mcp-Session-Id` возвращается во всех последующих запросах к нему; транспортный отказ называет метод, на котором он произошёл.
- [ ] `MCP_HEADERS_JSON` с любым регистром не переопределяет transport-managed заголовки session, protocol, method, name, content type или accept и отклоняется при startup как `CONFIG_INVALID`; все MCP timeout принимают только конечные значения допустимого знака.
- [ ] Новый запрос переживает scale-to-zero MCP: workflow и admission capability ceiling видны до сети, один durable deadline ограничивает discovery всех серверов и recovery без расхода model/tool budget, session изолирован по run, stale session очищается, отмена прерывает ожидание, а permanent auth/protocol error не повторяется. После deadline optional server исключается с `MCP_CONNECTION_FAILED`, но Task продолжает работу. Recovery после успешного discovery использует сохранённый catalog и отдельный durable reconnect deadline, не меняет EffectiveConfig, автоматически повторно подхватывает безопасное состояние после истечения старого lease без клиентского resubscribe и не оставляет повторяемый `CHECKPOINT_INVALID`; неразрешённое passive wait не запускается; resolved wait возобновляется один раз. Cancel до workflow admission не теряется по локальному timeout, а cross-worker cancel сначала durable сохраняет intent и получает terminal `canceled` только от fenced lease-owner/recovery. Read-only transport failure возвращается модели, неизвестный outcome mutating `tools/call` не повторяется. Инструмент считается изменяющим по умолчанию независимо от имени и server annotations; только явное совпадение с доверенным `MCP_READ_ONLY_TOOLS` разрешает обработать неизвестный transport outcome как ошибку только для чтения.
- [ ] Ответ выбирается по `id`: нотификации и чужие `id` в event stream пропускаются, и `tools/call` возвращает данные, а не пустой результат.
- [ ] `isError: true` в результате `tools/call` доходит до модели как failed tool result с текстом сервера, а не как успешный пустой output.
- [ ] `MCP_ALLOWED_TOOLS` принимает и голое имя тула, и форму `server.tool`; подключённый сервер без единого разрешённого тула порождает предупреждение с его именем.
- [ ] Production без valid `DATABASE_URL`, ожидаемой PostgreSQL schema или database readiness не открывает A2A listener и не использует in-memory/SQLite fallback.
- [ ] Соединение, закрытое сервером во время простоя, не доходит до caller ошибкой: пул проверяет и заменяет его, а следующий запрос выполняется успешно.
- [ ] Production serving credential не имеет DDL path: `DATABASE_AUTO_MIGRATE=true` отклоняется, а separate migration credential/app role дают только exact DML grants.
- [ ] A2A Tasks, events, checkpoints и audit переживают restart и остаются tenant/owner scoped в одной PostgreSQL transaction boundary.

- [ ] `startup.configuration` описывает runtime mode, built-ins, MCP-серверы с allowlist, удалённых агентов с причинами отказа и storage; `capabilities.resolved` показывает обнаруженные и разрешённые MCP-тулы по серверам. Секреты в обеих записях отсутствуют.
- [ ] `startup.configuration` выводится и тогда, когда логирование настраивает внешний ASGI-сервер после сборки приложения; неподключившийся MCP-сервер получает отдельную запись с кодом ошибки и отличается в `capabilities.resolved` от подключённого с пустым каталогом.
- [ ] `startup.configuration` называет OTLP endpoint для traces, metrics и logs по отдельности и булев признак наличия credentials; значение ключа отсутствует ни в каком виде.
- [ ] Старт печатает инвентарь переменных окружения короткими нумерованными строками `i/N`: те, к которым обращался старт, и отдельно присутствующие, к которым он не обращался, обе группы со состоянием `set|empty|missing`; ни одно значение не выводится.
- [ ] `startup.configuration` перечисляет configured owner registry отдельно от подключившихся и содержит причину отказа каждого неподключившегося; userinfo из URL удаляется.

## OpenTelemetry

- [ ] Traces, metrics и logs создаются OTel SDK и экспортируются OTLP.
- [ ] W3C Trace Context проходит через A2A, queue, MCP и background/subagent tasks без влияния на authorization.
- [ ] Incoming A2A call создаёт ровно один agent execution trace с root `core_agent.task.execute`; отдельные transport/submission traces отсутствуют, валидный incoming W3C parent продолжается, а независимая background/durable работа использует новый trace со Span Link.
- [ ] Core имеет MCP client span; Memory Service продолжает W3C trace и владеет BM25/vector/graph/rerank/NER spans.
- [ ] OTel semantic-convention version pinned; custom attributes используют `core_agent.*`.
- [ ] Content/arguments/results/system instructions выключены в telemetry по умолчанию.
- [ ] При privileged content capture Phoenix получает visible reasoning в OpenInference `message.contents` с `type=reasoning`; без capture reasoning text отсутствует, но safe reasoning token count сохраняется.
- [ ] Metric labels bounded и не содержат IDs, prompt, path, command, entity или memory text.
- [ ] Collector outage не повреждает Task/audit; telemetry drops наблюдаемы.

## Security, tenancy и data lifecycle

- [ ] Prompt injection из profile/file/skill/MCP/A2A peer не меняет kernel/host policy и не раскрывает secret.
- [ ] Tasks, terminal sessions/workspaces, connections, Markdown, indexes, graph, artifacts и telemetry tenant-scoped.
- [ ] Remote extensions проверяются по integrity/trust/revocation policy.
- [ ] Coordinated deletion очищает Core cached/transcript copies, а Memory Service — Markdown и derived indexes.
- [ ] Public errors безопасны, стабильны и отображаются в A2A semantics.

## Сквозные сценарии

1. **A2A background:** клиент отправляет Message с `return_immediately`, закрывает stream и позже получает тот же Task result через subscribe/push.
2. **Compaction:** working context достигает 90%, сжимается до 10–15%, не считая system/tools, и сохраняет pending Task.
3. **Memory disabled:** AgentConfig отключает встроенную память, и model не видит memory tools.
4. **Hard line limit:** update на 201+ строк отклоняется без изменений и рекомендует split; следующий явный split создаёт несколько valid Markdown files.
5. **Memory update:** agent находит существующий file через `core_memory_search`, commit обновляет NER/graph, hybrid search возвращает новую revision.
6. **Shared memory:** parent явно передаёт child тот же memory scope и явный tool allowlist; child commit уведомляет parent.
7. **Focused delegation:** child видит только capabilities из EffectiveConfig/delegation allowlist и возвращает обычный text result.
8. **Concurrent work:** main продолжает задачу, пока два child/background Tasks выполняются, затем обрабатывает notifications без busy polling.
9. **Parallel terminals:** main и два child одновременно работают в разных PTY одного чата, не смешивают output и завершают только свои process groups; общий workspace сохраняется, пересекающиеся изменения координируются либо изолируются в scratch-копиях с проверяемым merge.
10. **OTel causality:** Core MCP client и Memory Service indexing spans находятся в одном distributed trace без content leakage.
11. **Внешнее действие:** MCP write переживает recovery и выполняется ровно один раз.
13. **Live steering:** пока Task выполняет model/tool loop, два follow-up Messages приходят с тем же `taskId`, сохраняют committed order и учитываются агентом до terminal result без дублирования уже выполненного side effect.

## Definition of Done

- Все критерии и сквозные сценарии проходят в CI, integration, chaos и eval suites.
- A2A schemas и examples проверяются против одного источника типов.
- Recovery tests доказывают отсутствие duplicate side effects и потерянных notifications.
- Memory Service rebuild test удаляет derived indexes и получает эквивалентный searchable graph из Markdown.
- Security review охватывает A2A, model, MCP, skills, memory graph, Bubblewrap/egress границу одного Kubernetes Pod, subagents, OTel и tenancy.

## Enterprise migration и transport trust boundary

- [ ] Корпусы памяти с одинаковыми agent/owner ID разных trusted tenants изолированы в memory и PostgreSQL после restart; child получает только делегированный исходный scope. Known session-A ID недоступен read/update/split/delete из session-B или user namespace; отказ не меняет ни contents, ни revision. Migration23→24 сохраняет legacy rows под пустым tenant без автоматического присвоения; новые corpus не читают их, old image fail closed на новой schema.
- [ ] SDK/path/JSON-RPC tenant не перезаписывает trusted deployment company: mismatch отклоняется, пустое SDK значение сохраняет verified tenant.
- [ ] Legacy identity явно сопоставляется issuer/sub; неизвестный owner остаётся изолированным, исходные rows/blobs и digests сохраняются при migration.
- [ ] Versioned wait/cron/file migration сохраняет deadlines, closed outcomes и visibility; unknown dispatched side effects не replay-ятся.
- [ ] Перенос входящих files предшествует удалению artifact tools; прежних named artifact данных для переноса нет, отдельный export/import utility не поставляется. Три tools и исключительно их adapters/dependencies/config удалены из приложения без удаления внешних storage resources; устаревшие tool/env настройки диагностируются без вывода values. Owner registry import не перезаписывается ENV после cutover; rollback после enterprise writes требует безопасного reverse migration/backup boundary.

- [ ] Explicit remote registry import использует bounded version1 operator file,
  deployment company и database-authenticated migration actor; все encrypted peers
  коммитятся одним batch без discovery/forwarding. Повтор или nonempty registry
  отклоняются без перезаписи owner edits; invalid/key/duplicate-name/DB failure
  не оставляет pointers или revisions. Другой tenant и legacy Task/chat ownership
  не меняются; serving restart/ENV не запускают import. DB error не раскрывает
  values и возвращает `REMOTE_IMPORT_FAILED`.

## Enterprise v1: обязательные сценарии

Все ENT-AC ниже входят в текущий release candidate для исходного Keycloak/POSIX
профиля. В явном Cloud.ru profile caller identity, login/provisioning и storage
уточняются CLD-AC ниже; остальные policy, isolation и durability criteria
остаются обязательными. Перенос критерия не является автоматическим доказательством: статус исполнения фиксируется отдельно в implementation-status.

| ID | Дано / действие | Наблюдаемый результат |
| --- | --- | --- |
| ENT-AC-01 | Два владельца с нужной ролью входят в UI | Оба видят общие чаты и могут принимать разрешённые решения |
| ENT-AC-02 | Внешний агент отправляет решение HITL | Доступ отклонён, состояние approval и вызова не меняется |
| ENT-AC-03 | Агент B использует task/context/file ID агента A, в том числе в list/stream | Данные и существование чужих объектов не раскрываются; собственные доступны |
| ENT-AC-04 | Токен отозван, истёк либо introspection недоступна | Новый защищённый запрос не получает доступ |
| ENT-AC-05 | Токен сервисной учётной записи заменён | Новый валидный токен видит прежние собственные задачи |
| ENT-AC-06 | Инструмент запрещён через UI | Он отсутствует в следующем каталоге; прямой, nested Python и delegated вызовы блокируются |
| ENT-AC-07 | Подключён новый разрешаемый platform-ой инструмент | Он требует HITL до выполнения первого вызова |
| ENT-AC-08 | Владелец разрешает конкретный вызов | Выполняется согласованный вызов с исходными аргументами, без повторного dispatch из-за дубля решения |
| ENT-AC-09 | Отказ либо 24 часа без решения HITL | Side effect не выполняется; модель получает результат и может продолжить |
| ENT-AC-10 | Сервис перезапущен во время HITL | Запрос и прежний deadline восстановлены, повторного вызова нет |
| ENT-AC-11 | Через UI и A2A приходят вложения в сумме на лимите и на байт больше; владельцы меняют общий лимит через settings CAS | Первый набор доступен в правильной папке после проверки; превышение отклонено; scope сохранён. Default 25 000 000 bytes; external не меняет настройку, старый timeout-only PUT сохраняет лимит, уже принятые manifest не переоцениваются |
| ENT-AC-12 | Имя файла содержит traversal либо путь ведёт через symlink к чужой папке | Чужие данные не читаются и не перезаписываются |
| ENT-AC-13 | Агент выдаёт файл через UI/A2A | Авторизованный получатель получает файл; другой внешний агент доступа не получает |
| ENT-AC-14 | Два одновременных запроса создают задачи в одном свободном чате | Выполняется одна; другая сохранена и возвращена как `failed` с `CONTEXT_BUSY` |
| ENT-AC-15 | Новая задача поступает в чат с ожидающей HITL/remote задачей | Новая попытка `failed`; старая задача и её ожидание продолжаются |
| ENT-AC-16 | Follow-up приходит во время одного tool call | Вызов завершается; перед следующим model call присутствуют результат и затем новое сообщение |
| ENT-AC-17 | Один follow-up доставлен повторно либо попал в гонку с завершением | Нет двойной доставки или молчаливой потери принятого сообщения |
| ENT-AC-17a | Follow-up приходит во время HITL/remote ожидания | Сообщение сохранено, model call не запускается; после завершения ожидания сообщение присутствует перед следующим model call |
| ENT-AC-18 | Remote Task долго ждёт своего пользователя, stream закрывается и наш сервис перезапускается | Родитель остаётся нетерминальным; ожидание восстанавливается без повторной отправки задания |
| ENT-AC-19 | Срок remote wait истёк, затем тот же wait вызван снова, наступил очередной polling tick или произошёл restart | Немедленный окончательный timeout, прежний deadline не продлевается; новые запросы результата и повторные подключения не выполняются, CancelTask не отправлен |
| ENT-AC-20 | Remote completion и timeout происходят одновременно | Зафиксирован один исход ожидания и одно продолжение |
| ENT-AC-21 | Наступило сохранённое время пробуждения после restart | Продолжение запускается для проверки события, событие не объявляется свершившимся автоматически |
| ENT-AC-21a | Timer или local task wait сохранён; worker/сервис перезапущен до либо после resolve | Workflow lease, scheduler claim и execution thread освобождены; outcome применяется один раз к исходному call без повторного model/tool charge; новый follow-up будит только timer, duplicate не будит следующую generation |
| ENT-AC-21b | Joined child приостановлен; родитель перезапущен либо отменён | Child admission и parent wait атомарны; восстановление использует прежний child ID, required child остаётся nonterminal во время ожидания; cancel закрывает family waits и не теряет принятый inbox |
| ENT-AC-21c | Старый SDK Task snapshot или очередной Artifact chunk приходит после более нового workflow transition | Canonical state не понижается, private устаревший status message не возвращается; SDK chunk aggregate не изменяется reconciliation-ом и canonical final Message v2/legacy Artifact не дублируется |
| ENT-AC-22 | Наступил cron tick при активном предыдущем запуске | Нет нового выполнения и очереди; в чате виден immutable пропуск со stable cursor, включая пустой чат до первой Task; чтение не запускает модель |
| ENT-AC-23 | Инструмент создания cron запрещён | Модель его не видит; владелец всё ещё управляет расписаниями через UI |
| ENT-AC-24 | Следующий cron-запуск начинается после нескольких compaction | Модель получает актуальные решения и подтверждённые результаты прежних запусков |
| ENT-AC-25 | Позднее уточнение отменяет раннее решение; история суммаризируется несколько раз | Актуально позднее решение, план не превращён в выполненное действие |
| ENT-AC-26 | Summary потеряло упоминание запрета/закрытого ожидания | Структурированная policy/state всё равно блокирует действие или повторный wait |
| ENT-AC-27 | Guardrail передаёт подозрительный материал владельцу | До решения материал не используется для защищаемого продолжения; одобрение не расширяет права |
| ENT-AC-28 | Artifact tools и dedicated backend отключены/удалены | Их нет в catalog/config/dependencies; входящие файлы и обычные A2A outputs продолжают работать |
| ENT-AC-29 | Команды двух чатов одновременно работают через Bubblewrap | Каждая читает и изменяет свой `/workspace`; содержимое соседнего чата недоступно |
| ENT-AC-30 | Python, shell и их потомки пробуют чужие пути, `/proc`, сокеты и platform secrets | Ограничения сохраняются для всех процессов; чужой broker и данные недоступны; собственный разрешённый tool call работает |
| ENT-AC-31 | Bubblewrap отсутствует либо кластер запрещает обязательную изоляцию | Недоверенная команда не запускается; возвращается ошибка окружения без fallback к обычному процессу |
| ENT-AC-32 | После записи файла окружение исполнения и pod пересозданы с тем же постоянным томом; запускаются root, child и background/recovered команды | Тот же authenticated chat binding получает прежний файл в `/workspace`; соседний чат его не видит; owner, работающий в external чате, сохраняет его binding. Непривязанный legacy run отклоняется, старый base snapshot не перезаписывает постоянный чат |
| ENT-AC-33 | В sandbox выполняются requests/curl и установка пакета из публичного источника при разрешённой tool policy | Сеть работает, пакет записывается только в разрешённую область чата |
| ENT-AC-34 | Процесс обращается к внутреннему IP, metadata endpoint, серверному localhost, соседнему окружению либо перенаправляется туда с публичного URL | Доступ запрещён, в том числе при прямом socket-соединении, без proxy env и при изменении DNS-ответа |
| ENT-AC-35 | Используются IPv4, IPv6 и альтернативные записи адресов при работающих серверных интеграциях | Сетевая граница не обходится; сервер сохраняет свой разрешённый доступ к БД/Keycloak/MCP/A2A |
| ENT-AC-36 | Контроль выхода sandbox в сеть не удалось запустить или он отказал | Неконтролируемый доступ к сети pod не появляется |
| ENT-AC-37 | Владелец отклоняет подозрительное сообщение, файл или tool result | Модель получает уведомление об отказе без содержимого; задача может продолжиться по разрешённым данным и автоматически не становится `failed` |
| ENT-AC-38 | После отказа агент пытается получить материал через summary/retrieval/memory, чтение файла либо другой инструмент | Отклонённое содержимое не выдаётся; разрешённые материалы того же чата остаются доступны. Поздний отказ по точному материалу до publication исключает ранее одобренный batch, включая recovery после rename до DB commit; private owner history сохраняет bytes и оба решения, повторное одобрение не раскрывает batch |
| ENT-AC-39 | Владелец не решил запрос guardrails до настроенного deadline; для настройки по умолчанию прошло 24 часа | Материал остаётся закрытым; модель получает уведомление о timeout и может продолжить; задача автоматически не становится `failed` |
| ENT-AC-40 | Во время ожидания guardrails сервис перезапущен либо решение владельца совпало с deadline | Срок не начинается заново; фиксируются один исход и одно продолжение, follow-up не теряются |
| ENT-AC-41 | Требуемая проверка guardrails завершилась ошибкой или детектор недоступен | Материал закрыт; владелец получает запрос «Не удалось проверить»; отказ и timeout обрабатываются по GUARD-02/03. Пять запросов детектора могут выполняться одновременно на serving process; при занятых пяти слотах следующая проверка получает unverified/detector_busy без сети и начисления попытки. Timeout не освобождает слот до фактического завершения запроса; освобождённый слот доступен новой проверке |
| ENT-AC-42 | Владелец помечает инструмент как доверенный для guardrails | Детектор не вызывается для результатов этого инструмента; tool policy, HITL, schema validation и изоляция продолжают действовать. Новые аргументы модели не классифицируются независимо от метки |
| ENT-AC-43 | Новый MCP tool или внешний caller объявляет себя безопасным через metadata/текст | Исключение не включается; доверенную настройку может изменить только владелец |
| ENT-AC-44 | Детектор недоступен, а для конкретного инструмента действует исключение TOOL-02 | Для исключённой области нет вызова детектора и запроса guardrails из-за его ошибки; необходимость HITL определяется независимо |
| ENT-AC-45 | Проверяется сформированный запрос к настраиваемой модели guardrails и подставляется заданный вердикт | У детектора отдельный контекст без tools, секретов и чужой истории; результат обрабатывается по GUARD-01–04. Оба HTTP bindings передают полную строгую схему verdict при default и override настройке; унаследованный формат заменяется без изменения основной модели. Отказ принять схему не вызывает fallback без схемы. Нет clear при дополнительных/повторяющихся JSON-полях, tool calls, отсутствующем признаке завершения, непроверенном хвосте или исчерпанном budget/deadline; попытка сохраняется до сети, поздний clear игнорируется. Точность классификации на данных не является критерием |
| ENT-AC-46 | Один вызов ждёт HITL; владелец включает автоматический режим, затем поступает новый вызов в другом чате | Старый запрос ждёт решения с прежними аргументами/deadline; новый не требует tool HITL, остальные проверки сохраняются |
| ENT-AC-47 | Во время задачи внешнего агента наш агент задаёт владельцам вопрос и получает ответ | Владельцы видят переписку в чате; внешний caller видит факт ожидания и итоговый ответ, но не внутреннюю переписку через историю, GetTask/ListTasks, stream, push, файлы, артефакты или metadata, в том числе после восстановления |
| ENT-AC-48 | Внешний caller присылает follow-up, пока ожидается ответ владельца | Сообщение сохраняется по правилам follow-up, но не считается ответом владельца и не закрывает ожидание |
| ENT-AC-49 | Владельцы не отвечают на уточняющий вопрос до настроенного срока, в том числе после restart; ответ может конкурировать с timeout | По умолчанию срок равен 24 часам и не сбрасывается; при timeout агент получает «ответ не получен вовремя» и может продолжить без этих сведений, без автоматического failed; завершение ожидания и возобновление однократны |
| ENT-AC-50 | После зафиксированного remote timeout приходит ответ на ранее отправленный запрос или SSE/push-событие, если этот канал используется | Результат игнорируется, не публикуется в чате и не передаётся модели; исход ожидания не меняется, повторного продолжения нет |
| ENT-AC-51 | В нескольких чатах вызовы одного инструмента ждут HITL; владелец запрещает инструмент, затем приходят поздние разрешения либо происходит restart | Все ожидавшие вызовы закрыты без исполнения с результатом «инструмент запрещён»; задачи могут продолжиться, закрытые запросы не восстанавливаются и повторного продолжения нет |
| ENT-AC-52 | Сервис недоступен в один или несколько моментов cron-расписания, затем восстанавливается | Пропущенные запуски не выполняются, включая один компенсирующий; scheduler ждёт следующего момента расписания, в чате виден пропуск. Уже принятая до сбоя задача восстанавливается по общим правилам, без повторного создания. Startup/leader смена/gap >60 секунд фиксируют cutoff; healthy lateness >60 секунд также пропускается. Crash между admission и event/next_due не оставляет частичный commit; duplicate/unknown COMMIT и два coordinators не создают вторую Task |
| ENT-AC-53 | Владелец отключает или удаляет расписание с активной задачей, в том числе ожидающей HITL; затем наступает cron tick или происходит restart | Новые задачи по расписанию не создаются; принятая задача продолжает работу или восстанавливается, её чат, файлы и HITL доступны; отмена задачи требует отдельного действия |
| ENT-AC-54 | Создаются расписания с поясом по умолчанию и явно выбранным поясом; сервис перезапускается с другим системным timezone | Default равен Europe/Moscow, явный выбор сохраняется; моменты запусков определяются поясом каждого расписания, UI показывает его и ближайший запуск; неверный идентификатор отклоняется Диалект содержит пять полей и поддерживает names/lists/ranges/positive steps, Sunday 0/7 и DOM/DOW OR; invalid/impossible выражения отклоняются безопасно до сохранения. Gap пропускается, fold выполняется только в первом occurrence; Berlin/Lord_Howe и строго следующий UTC instant проверяются на реальном закреплённом parser release. |
| ENT-AC-55 | Владелец нажимает «Запустить сейчас» в свободном чате расписания | Создаётся обычная задача с сохранённым prompt в том же чате и workspace; расписание и время следующего cron-запуска не сдвигаются; действуют обычные HITL и guardrails. Owner API и tool имеют idempotent receipts/CAS, foreign403/404 и strict validation; create без context создаёт пустой чат, tool использует текущий чат без выбора identity. Retry run-now после edit/disable/delete возвращает прежнюю Task; late fenced tool completion не создаёт schedule |
| ENT-AC-56 | В чате уже есть активная задача либо ручной запуск конкурирует с другим стартом | UI блокирует кнопку при известной занятости; сервер атомарно допускает не более одной активной задачи, проигравшая ручная попытка получает failed/CONTEXT_BUSY без очереди; cron tick при активном ручном запуске пропускается |
| ENT-AC-57 | Во время активной задачи владелец меняет prompt или время расписания; задача выходит из ожидания либо восстанавливается после restart | Текущая задача сохраняет исходное задание; будущие автоматические и ручные запуски используют новые параметры; при конкурентном приёме сохраняется одна согласованная версия, редактирование само не запускает задачу |
| ENT-AC-58 | Владелец открывает очистку workspace, выбирает файлы и подтверждает удаление | До подтверждения видны имена, пути, размеры, даты, выбранный перечень и общий объём; preview/download проверяют owner/company и original chat binding, не запускают модель или workflow и не создают отсутствующий workspace. Cursor связан с каталогом/фильтром/server timestamp; unsafe files/control manifests не выдаются, traversal limit даёт явную ошибку. Удаляются только выбранные неизменившиеся файлы; UI показывает удалённые, пропущенные и ошибки, чат и история сохраняются |
| ENT-AC-59 | Между просмотром и удалением выбранный файл изменён, подменён ссылкой либо добавлен новый файл; после successful capture/unlink сервис перезапускается до DB commit | Изменённый или заменённый файл требует нового просмотра, включая same-inode запись с прежними size/mtime; новые файлы и данные вне выбранного workspace не удаляются. Immutable intent/private capture и deletion proof сохраняются до эффекта; recovery не делает blind unlink и не считает missing path доказательством удаления. Подмена сохраняется/восстанавливается без перезаписи, неоднозначность требует reconciliation. Request retry возвращает тот же receipt; подтверждённо удалённые файлы не восстанавливаются из старого снимка |
| ENT-AC-60 | Владелец задаёт фильтр N дней при наличии более старых, более новых файлов и файла ровно на границе, затем выбирает часть результата | Показаны только файлы, не изменявшиеся более N суток по серверному времени; изначально ничего не выбрано, удаление требует ручного выбора и подтверждения; невыбранные файлы сохраняются |
| ENT-AC-61 | В чате есть активная задача, включая ожидание, либо запуск задачи конкурирует с очисткой workspace | Просмотр и фильтрация доступны; UI и сервер блокируют удаление при активной задаче без создания/очереди intent; после завершения нужен новый ручной запрос. Cleanup и root admission используют canonical chat lock. Незавершённый принятый cleanup блокирует новую Task до admission, включая после сбоя; existing duplicate receipt неизменен. Очистка не пересекается с исполнением задачи; GET receipt не возобновляет мутацию |
| ENT-AC-62 | Через UI или A2A поступает файл с занятым именем, в том числе несколько таких вложений одновременно; owner перечитывает историю после публикации | Существующие файлы сохранены; новые получают разные свободные имена с суффиксами без перезаписи при гонке; UI, результат приёма и контекст агента содержат фактические имена. История сохраняет scoped actual name/path/size/digest initial и follow-up файлов; pending/excluded имена остаются только в owner review |
| ENT-AC-63 | В сообщении UI или A2A одно из нескольких вложений не загрузилось, не сохранилось либо превышен общий лимит, в том числе после загрузки первых файлов | Всё сообщение отклонено с причиной; текст и частичные вложения не передаются агенту и не публикуются в workspace, включая после restart; существующие файлы и активная задача сохранены |
| ENT-AC-64 | Агент готовит исходящее сообщение UI или A2A с файлами, каждый из которых меньше лимита, но сумма больше него | Отправка отклоняется до частичной публикации; агент получает ошибку с размерами и может уменьшить набор, исходные файлы сохранены; используется общий с входящими вложениями лимит, default 25 МБ. Для legacy runs core_response_files заменяет весь набор и [] очищает; для v2 он append-ит file-only Artifact немедленно, [] no-op. Ошибочный новый batch сохраняет прежний; receipt/manifest атомарны. Live/GetTask/SSE/recovery выдают те же frozen ordered snapshots после изменения originals, включая budget-partial result; corrupt batch не выдаётся частично. Owner download повторно проверяет Task/chat scope, одинаковые bytes/известный ID другого чата не открывают файл |
| ENT-AC-65 | История чата стареет, контекст суммаризируется, владелец очищает выбранные файлы workspace | Полная история сообщений остаётся доступной с прежними правами; автоматического удаления по возрасту нет, очистка затрагивает только выбранные файлы. Owner history API страницы включают все canonical roots, local transcript и каждый final result один раз; provider replay/hidden reasoning исключены. Принятое unread уточнение видно сразу, delivery/rejection не меняет ID и не дублирует entry. Guard material выводится placeholder с scoped review, без raw fallback. Cursor сохраняет позицию после новых roots, append, доставки и compaction; invalid/foreign cursor, child или unmapped legacy run не открывают данные. Reads не вызывают модель/tools/classifier, не меняют waits/budget; external/dual-role403, другая company404. Legacy snapshots без новой provenance читаются детерминированно без потери transcript |
| ENT-AC-66 | Caller повторяет создание с тем же messageId, в том числе одновременно, после restart или замены токена; отдельно повторяет его с изменённым содержимым | При том же содержимом возвращается одна и та же Task без повторного выполнения и копий файлов; изменённое содержимое даёт конфликт; текущие права и scope проверяются |
| ENT-AC-67 | Авторизованный HTTP-ответ остаётся открытым дольше срока токена; затем приходит новый запрос по тому же TCP-соединению или новая подписка | Внутри открытого ответа повторной авторизации и закрытия по истечению токена нет; новый HTTP-запрос проходит новую проверку и с недействительным токеном отклоняется; Task не отменяется |
| ENT-AC-68 | Загрузка отклонена или после сбоя остались временные файлы; рядом есть активные загрузки и принятые файлы, ожидающие guardrails | При обычном отказе временные файлы удаляются сразу, осиротевшие остатки — в течение 24 часов с восстановлением очистки после downtime; активные и принятые данные сохраняются |
| ENT-AC-69 | Агент ждёт до 18:00, в 15:00 принято уточнение в ту же задачу | Ожидание завершается с причиной прихода сообщения, агент сразу возобновляется и получает уточнение перед моделью; прежний таймер не запускает второе продолжение и не считается подтверждением внешнего события |
| ENT-AC-70 | После пробуждения по сообщению агент снова вызывает ожидание, затем приходит дубликат сообщения, старое событие таймера или происходит restart | Новое ожидание сохраняется до собственного повода пробуждения; повтор уже принятого messageId и старый таймер не будят его, продолжение выполняется однократно |
| ENT-AC-71 | Агент возвращает Markdown/GFM и блоки Mermaid; владелец открывает результат и перечитывает историю | Заголовки, списки, таблицы и код форматируются; валидная диаграмма отображается статическим изображением, которое не сбрасывается при обновлении истории/статусов без изменения текста. Ошибочная диаграмма оставляет исходник и сообщение. HTML/scripts/опасные ссылки и внешние изображения не исполняются и не загружаются; CSP сохраняет scripts/styles self и допускает blob только для изображения |

Проверки конкурентности должны действительно создавать гонку. Перезапуски
проверяются с PostgreSQL и сохранёнными файлами. Для суммаризации нужны случаи
с поздними исправлениями, длинными логами и повторной compaction; проверка
наличия заголовков summary недостаточна. Для guardrails проверяются механика
интеграции и обработка заданных вердиктов; оценка качества детектора на наборе
данных исключена из объёма по [контракту детектора](security-and-reliability.md).


### Дополнительные owner-interaction criteria

- OWNER-AC-OPTIONS: `core_ask_owner` принимает вопрос без вариантов, с `[]` и с
  1–8 уникальными непустыми строками до 500 символов; неправильные типы, пустые
  или пробельные строки, duplicates, слишком длинный или большой список
  отклоняются до создания owner wait. Direct и Python broker используют одну
  схему. Immutable question/options и прежний deadline переживают recovery;
  digest не допускает ответа на изменённый subject. UI предлагает ровно один
  выбор либо «Свой ответ», без предвыбора и автоматической отправки. Отдельная
  кнопка отправляет точный вариант или собственный текст; обычный owner API
  также принимает свободный ответ при наличии вариантов. Legacy вопросы без
  `options` сохраняют прежний интерфейс и result shape. Проверены клавиатура,
  длинные варианты на mobile, busy/closed, сохранённый ответ после reload,
  owner-only/company isolation и отсутствие вариантов во внешних A2A projection.

- ENT-AC-74: вызовы с одинаковым call ID в разных Tasks не смешиваются;
  каждый вызов/результат отображается одной обновляемой карточкой. Успешные
  последовательные действия сворачиваются, ошибка/неопределённый исход/активный
  HITL видны. Компактные карточки открывают модальное окно с аргументами,
  техническими данными и выводом с настоящими переносами строк. Native browser
  проверяет открытие мышью/клавиатурой, Escape/закрытие, возврат фокуса,
  сохранение прокрутки, ограничение окна на mobile и видимость ошибки в ленте.
  Available служебные placeholders без review/outcome/attachments/response_files
  сохраняются в history API, но не создают пустых блоков или разрывов групп в UI.
  Pending/rejected/timed-out/unprocessed entries и terminal outcomes не скрываются.
- ENT-AC-75: runtime statuses/owner waits/remote waits/cancel/failure отображаются
  независимо от текста модели. Разрешение не означает выполнение. В шапке
  доступны остановка/обновление, над composer — переход к активному запросу.
  Polite announcements не крадут фокус; нормальная синхронизация не шумит.
  Initial Task fetch и terminal SSE EOF не показывают потерю соединения;
  SSE-only failure при рабочем GetTask сохраняет тихий polling. Ошибка GetTask
  показывает восстановление, успешное чтение снимает его.
- ENT-AC-76: active approval показывает фактическое действие и параметры,
  deadline с timezone и действия allow/reject; approved/rejected/timeout карточки
  скрыты, owner question answers доступны в свёрнутой записи «Ответ отправлен»
  без прежней формы/deadline. Раскрытие мышью и клавиатурой показывает полный
  вопрос и ответ; timeout/cancelled имеют отдельные точные подписи. Решения
  не стираются из хранения.
  Активный HITL заменяет только соответствующий незавершённый вызов по task/call ID;
  другие вызовы и их ошибки сохраняются. Вопрос читается на всю ширину карточки.
  Разрешение выполнить core_ask_owner не отправляет ответ владельца. Технические
  раскрытия и свойства файлов доступны через шестерёнку с подсказкой и именем.
  Шестерёнка стоит после «Скачать» у файла, после «Отклонить» у HITL;
  guardrail ставит просмотр материала и доступное скачивание после allow/reject,
  без шестерёнки и технического раскрытия. Просмотр показывает источник/известную
  команду, понятную причину проверки и содержимое без review-обёртки. Terminal/Python
  вывод сохраняет переносы и непустой stderr; structured business payload не
  теряет свои поля. Служебные ID/revision/timestamps/digest/токены/время проверки
  не отображаются. Длинный текст читается полностью в доступном scroll region;
  HTML остаётся текстом, не исполняется. Кнопки имеют одинаковую высоту и выравнивание,
  на мобильном экране переносятся без наложений. Раскрытия работают с клавиатуры.
  В окне действия код и stdout/stderr выровнены по общей ширине, отступы секций
  одинаковы; длинные технические данные остаются внутри прокручиваемого окна.
  Сразу после отправки текст и имена вложений видны локально, поле очищено;
  три пульсирующие точки сохраняются при обычных вызовах инструментов и заменяются
  live-ответом. На terminal, HITL, ошибках и внешних ожиданиях точки скрыты;
  prefers-reduced-motion отключает движение. Нет служебной заглушки pending/queued
  или обычного статуса работы в шапке. Сопоставление по input_message_id не
  дублирует сообщения, включая remount нового чата и одинаковые тексты уточнений.
  Неопределённый send повторяется с прежним ID/телом, явный отказ возвращает
  черновик; отклонённый материал не подменяется локальной копией.
- ENT-AC-77: поиск/источник/режим/отключённые проверки фильтруют компактный список
  tools. Положительный checkbox сохраняет `guardrails_exempt = !checked`;
  сохранение одного правила сохраняет CAS и не изменяет соседние правила.
  Нет технического режима или пакетного сохранения.
- ENT-AC-78: original owner text и карточки входных/выходных файлов показаны без
  системного suffix/пути в основном содержимом. Safe preview/download используют
  прежние owner APIs; HTML не исполняется, unsupported types предлагают download,
  chat switch/logout прерывают запрос. Панель разделяет входные/выходные файлы,
  даёт переход к сообщению и доступ к прежней ручной очистке workspace.
- ENT-AC-79: название по первому запросу/файлу и ручное переименование сохраняются
  после refresh/restart; company owners видят общее имя. External/dual role403,
  другая company404, malformed title400, stale revision409. UUID не занимает
  строку чата и доступен через copy action; task/workspace scope неизменен.
  Список показывает названия без даты/времени обновления, сохраняя порядок
  по последнему обновлению и отметки, требующие внимания.
- ENT-AC-80: shell/sidebar/message authors и login/logout не показывают имя
  продукта или логотип; вкладка имеет нейтральное название «Чат». Composer растёт от двух строк, aggregate limit имеет понятные units;
  новые события не сбрасывают прокрутку читающего пользователя. Видна кнопка
  новых сообщений, focus/последняя запись не закрыты sticky elements; desktop,
  mobile, keyboard и contrast thresholds проверены в реальном браузере.
  Поля имени и адреса подключения выровнены и имеют одинаковую высоту, даже
  при переносе подсказки имени; на mobile они вертикальны и не переполняют форму.

- ENT-AC-81: owner подтверждает удаление чата; он исчезает у всех владельцев
  company после refresh/restart, cron атомарно отключаются. Active root409,
  external/dual403, foreign404; повтор200 без новых событий. Архивный context
  не принимает новые root/follow-up/rename/cron create/enable/run; сохранённые
  retries не создают новую работу. Старые Task/history/immutable files доступны
  только прежним scoped callers, identity и bytes не изменяются. Memory/PG
  race, migration и native browser подтверждают поведение.
- ENT-AC-82: Python source сохраняет whitespace и получает безопасную подсветку
  в ответе/tool parameters/file preview; код не исполняется, опасный HTML
  остаётся текстом. Markdown file preview рендерит таблицы/Mermaid с прежними
  ограничениями. Длинный preview не перекрывает свойства/следующие сообщения,
  раскрытие многих действий не делает ответ недоступным; оба scroll
  regions доступны с клавиатуры на desktop/mobile. Последовательность успешный
  вызов → ошибка → успешные вызовы → активный вызов образует один «Ход выполнения»;
  его свёрнутый заголовок показывает ошибки и текущее действие. Завершение вызова
  сохраняет раскрытие блока, HITL остаётся видимым снаружи. Одиночный вызов также
  находится в блоке; сообщения между вызовами сохраняют порядок.

- ENT-AC-72: владелец нажимает «Выйти» в реальном браузере с действующей
  Keycloak-сессией. SSO завершается; старый bearer даёт 401 на новом private
  request. UI показывает экран выхода без приватных данных и автоматического
  повторного входа, также после reload. «Войти» открывает обычный PKCE flow
  с формой авторизации. Logout не отменяет durable Tasks; auth expiry и access
  error не вызывают самостоятельного login redirect.
- ENT-AC-73: активные tool approval и guardrail requests видны в чате;
  canonical allowed/rejected/timeout outcomes скрывают их карточки, в том числе
  после history refresh/reload. Решения остаются доступны owner API и audit,
  ответы на owner questions остаются в раскрываемой истории чата, включая reload.
  Локальный deadline без persisted
  outcome не скрывает ожидание; разрешение не выдаётся за выполненное действие.

- [ ] CAS policy/settings не теряет конкурирующие изменения; новый origin под
  прежним alias требует HITL. Owner allow не закрывает прежний pending request,
  deny закрывает pending во всех чатах до нового dispatch.
- [ ] После HITL/restart изменённая MCP schema или исчезнувший tool не исполняются
  по прежнему подтверждению. Новые identities не расширяют принятые права;
  недоступный optional tool скрыт, исходные catalog и digest checkpoint сохранены.
- [ ] У Python prefix есть один проверяемый side effect, nested call ожидает
  решение, remainder имеет другой side effect. После restart/allow nested call
  выполнен один раз, prefix не повторён, remainder не выполнен; модель получает
  interrupted result. При reject/timeout nested call не исполняется.
- [ ] Неподтверждённый Python teardown и неизвестная nested mutation не становятся
  safe wait и не replay-ятся. Решение не позволяет обойти новый deny или ceiling.
- [ ] Python перехватывает исключение вложенного инструмента: неизвестный исход
  мутации остаётся `SIDE_EFFECT_UNKNOWN` и после подтверждённой очистки `ABORTED`;
  без неё сохраняется nonterminal барьер. Исчерпание бюджета остаётся partial
  с финализацией без tools. Отклонённое начисление фонового target не увеличивает usage.
- [ ] Root завершает ответ при работающем optional local child: terminal commit
  и допуск следующей Task в чат происходят только после остановки всего subtree.
  Cleanup failure оставляет чат занятым; чужой server receipt не считается очищенным.
- [ ] Потерявший lease worker не останавливает новую generation. Resume terminal
  intent после restart не повторяет команду; follow-up во время очистки запускает
  следующую команду в новой generation с сохранёнными файлами. Конкурентная очистка
  публикует ровно один непустой snapshot с прежними файлами.
- [ ] Вложенный joined delegate из Python возвращает результат child через
  исходный outer tool call. Изменение `allow` на `require_hitl` во время остановки
  создаёт approval с конечным настроенным timeout, по умолчанию 24 часа.
- [ ] Sentinel внутреннего вопроса/ответа отсутствует во всех external
  Get/List/Subscribe/stream/push/Artifact/metadata до и после restart; owner API
  сохраняет исходную внутреннюю переписку.

Remote scheduler proof LONG-01/02: memory и PostgreSQL проходят одинаковые проверки
claim/due/expiry/CAS. Checkpoint и terminal notification/outbox атомарны; после
crash с Send intent без remote ID повтор Send запрещён, с известным ID — только
GetTask. Race timeout versus result сохраняет ровно один исход/notification,
late claim не публикует материал; повтор wait/get/cancel после timeout не делает
network request. Shutdown не становится caller cancel; tenant/owner чужой
операции и mailbox имеют not-found/пустую scoped выборку. Старые local rows с
null checkpoint и их terminal semantics не меняются.

Remote runtime proof: pin destination revision/settings/message ID до HITL,
одна atomic admission локального handle и parent continuation; approved pending
call сохраняет binding при owner rotation/disable. Повторный call ID в новом
attempt создаёт новое намерение, recovery прежнего attempt возвращает прежний
handle. Top-level и вложенный Python используют один fenced dispatch flow.
Remote wait отклоняет любое явно переданное `timeout`, не продлевая deadline;
пустой разрешённый enabled registry скрывает Send в Agent Card и блокирует
новые поручения. Model catalog сохраняет Send только при наличии ранее принятых
операций с закреплённым reply binding; без них скрывает его. Scoped peer
credentials/root identifiers не наследуются извне.

Agent Card registry proof: owner/external карточки используют только enabled,
неудалённые metadata authenticated company и текущую tool policy. Пустой,
disabled/deleted/чужой registry и deny скрывают Send; разрешённая запись после
первой страницы включает его. Self-peer возвращает Card без вложенного HTTP
discovery, без модели и без расшифровки credentials. Недоступность peer или
его credentials не ломает Card; model catalog/dispatch сохраняют отказ по этим
причинам. Wire versions и private scope не меняются.

Remote file selection proof: workspace содержит выбранные и невыбранные файлы,
а final response имеет свой выбор. Только явный `core_agent_send_message.files`
передаётся в обеих bindings; отсутствие/[] передаёт один text Part. Invalid,
symlink/foreign path, отсутствующий или последний oversized файл дают zero Send
и zero handle, не меняют originals/final selection. После HITL/restart/изменения
originals отправляются frozen ordered bytes и empty file. Safe approval receipts
соответствуют selection digest; private refs не проходят в model/UI/public A2A.
v1 text-only jobs восстанавливаются прежним способом; v2 source root/child scope
не позволяет загрузить чужой manifest даже с одинаковыми bytes/известным ID.
Malformed/noncanonical/oversized remote batch не relay-ится частично. Human-wait
не импортирует ранние files. Completed terminal result и quarantine acceptance
атомарны при реальном PostgreSQL pool1; cancel/deadline/lease races не оставляют
accepted files. Root/child guard allow/reject/timeout/restart соблюдают общий
publication barrier без shadow Task или обхода canonical ancestry.

RMT-01, UI proof: при создании подключения видна подсказка о допустимых
символах и длине имени. Кириллица, пробелы и имя длиннее 128 символов
не отправляются серверу; допустимое имя с дефисом и подчёркиванием сохраняется.
Проверка выполняется в реальном браузере; server validation остаётся обязательной.
Кнопка «Удалить» требует подтверждения; отмена не отправляет запрос. Подтверждённое
удаление сохраняется после refresh/restart и скрывает регистрацию у всех владельцев
и из каталога модели. Отключённые подключения остаются видимыми. Повторное создание
того же имени получает новый ID; accepted operation продолжает использовать прежние
ID/revision/credential, включая после PostgreSQL restart. Старые current ID
не изменяются и не включаются заново; foreign/invalid/CAS запросы не удаляют данные.
Migration26→27 сохраняет существующие registry/revisions и legacy DELETE semantics;
serving role не получает DDL/DELETE, tombstone не возвращается в active state.
Browser proof включает потерянный ответ DELETE и поздний GET без слепого повтора
удаления или появления уже удалённой строки; поздний callback сохранения не закрывает
новый редактор. DELETE, завершившийся до durable `remote.pinned`, блокирует поздний
discovery прежней регистрации даже после создания нового peer с тем же именем.
Current check и pin атомарны; уже pinned HITL/remote operation продолжает прежний binding.

RMT-01, adaptive polling proof: при default interval300 первый GetTask назначен
через10 секунд от завершения Send; до возраста180 секунд повторные проверки
идут через10 секунд, от180 до780 — через30, после780 — через300. Проверки,
назначаемые перед границей фазы, ограничены этой границей. Более короткий
закреплённый интервал сохраняется. Временная ошибка чтения следует тому же графику;
длительность сетевого запроса учитывается по актуальному server/DB clock.
Recovery до `next_poll_at` не отправляет GetTask; новый PostgreSQL scheduler/pool
возобновляет среднюю и позднюю фазы по сохранённому началу без повторного Send.
Deadline не продлевается, проверка не назначается позже него; после terminal
outcome новых сетевых запросов нет. Изменение не расходует model/tool budget.

Remote progress proof: working/input-required/auth-required metadata появляются
в текущем root Task и scoped Get/List/Subscribe/push без raw peer data и чужих
операций. Pending wait, workflow version и model/tool usage не меняются;
одинаковый polling не повторяет status событие, stale SDK save не стирает progress.
Checkpoint/progress атомарны; malformed progress отвергается без изменения
revision/checkpoint. Timeout и terminal root подавляют позднее progress, PG
projection/push rollback сохраняет прежние гарантии terminal reconciliation.

Owner UI static proof: без bearer доступны только configured shell и
перечисленные compiled assets; credentials/private API остаются закрыты.
Unknown API/A2A/path/asset, traversal, symlink и внепакетный файл не получают
HTML fallback. CSP ограничивает connections deployment issuer origin,
scripts/styles self, shell не кешируется. Disabled client/absent build дают404.
Synthetic static fixture доказывает routing/security, но не SPA build,
реальный Keycloak login и browser interactions.

- ENT-AC-83: активация навыка действует в пределах canonical чата между root
  Tasks. Новый root перед первым model call получает полное тело текущей
  разрешённой закреплённой версии, даже после прежних initialized пустых roots
  или сбоя инициализации. PostgreSQL reopen и compaction сохраняют активацию;
  отключение скрывает инструкции, повторное включение восстанавливает активацию.
  Другой chat/tenant/owner scope и child не наследуют её; неверная lineage,
  activation source или непроверенный legacy lock дают безопасную ошибку.
  Исторический activation receipt не является источником новой инструкции.

- ENT-AC-84: поток модели owner-задачи достигает owner UI
  до окончания фактической генерации. Служебный сигнал списывает tool budget и
  закрепляет answer phase; следующий turn tools-free и списывает model budget.
  Reasoning, work/tool stream и private owner conversation не раскрываются.
  Реальный TCP и native browser доказывают первый prefix при заблокированном
  provider, накопительные scope/generation/sequence, passive reconnect без нового
  model call у owner A2A-клиента и однократную замену сохранённым итогом. В UI повторное
  открытие страницы использует canonical историю без восстановления preview;
  отправка из текущего окна включает live текст без сохранения признака в storage.
  Неполный Markdown остаётся
  plain text, finished answer получает безопасный существующий renderer.
  Superseded/retry/follow-up, cancel, HITL/recovery и budget exhaustion не создают
  ложного final, повторного side effect или бесплатного model/tool call. Legacy
  tools-free reply корректно воспроизводит прежние tool turns без расширения
  каталога. Restart сохраняет phase/usage; transient preview не обязан пережить
  рестарт и не становится history, push, Artifact или источником полномочий.

- EXT-AC-BUFFERED: external-owned root не получает `core_response_begin` и
  инструкцию для answer phase при наличии live reply sink/streaming adapter.
  Обычный финальный model response сохраняется canonical Message без control
  turn и token preview, включая подписку и owner-просмотр external Task.
  Явные `core_message_send`, Artifacts и lifecycle events продолжают работать.
  Prompt/metadata не меняют trusted режим. Owner-задача сохраняет прежний live
  ответ. Recovery external legacy answer phase сохраняет tools-free buffered
  завершение и бюджеты; pending control и новый вызов дают linked TOOL_UNAVAILABLE
  с продолжением loop. Нет скрытого исполнения control tool, повторных side
  effects, нового tool в каталоге или сброса уже выполненной работы.

- ENT-AC-85: owner UI «Доступ к агенту» выдаёт внешний доступ через текущую
  сессию владельца и реальный Keycloak; отдельные provisioning credentials и
  собственный API-key registry не нужны. Полный owner HTTP flow доказывает
  role/audience isolation, переданный owner bearer, сроки 1–365 дней, отсутствие
  credentials в metadata/errors, no-store и owner-only mutation; повторы и
  upstream failures не создают дубликаты или ложную выдачу. Замена сохраняет
  identity и закрывает прежний токен даже в ту же секунду, revoke закрывает новые
  запросы без удаления задач/файлов. Native browser доказывает accessible modal,
  count, single display/copy и отсутствие сохранённого токена после закрытия.
  Отдельное подтверждённое удаление через owner HTTP удаляет native client
  вместе с service-account user, убирает строку/число и закрывает старый токен.
  Задачи и файлы остаются доступны владельцам; новая одноимённая учётка
  не получает старый caller scope. External/foreign/owner-authority client
  не удаляется; native Keycloak и браузер доказывают реальный cascade,
  отмену подтверждения без DELETE и обновлённый список после успеха.

- ENT-AC-86: owners редактируют profile prompt, выбирают модель текущего provider
  и добавляют MCP URL/credentials, включают и отключают отдельные подключения.
  Реальные owner HTTP и браузер проверяют отдельное сохранение, CAS, secret
  withholding, недоступный provider list без потери текущего выбора и отказ
  external caller. PostgreSQL restart сохраняет настройки и encrypted immutable
  revision; новая Task использует их, admitted Task/recovery/child сохраняют свой
  закреплённый профиль, модель и exact MCP ceiling. При выключении новая Task
  не получает сервер; default tool HITL и deny dispatch остаются обязательными.
- ENT-AC-87: отправленное A2A-поручение имеет компактную canonical карточку,
  публичная переписка/файлы — в правой панели. Исходящие сообщения справа,
  входящие слева, обе стороны в скруглённой обводке. Markdown заголовки, списки,
  выделение и ссылки работают в обоих направлениях; raw HTML не исполняется.
  На узкой и широкой панели длинный текст/код/таблицы не расширяют её;
  неподтверждённая отправка остаётся явно помеченной. Настоящий peer transport и native
  browser проверяют выбор контрагента вместо задачи, единую хронологическую ленту
  нескольких поручений одному агенту, отдельную ленту другого агента и сохранение
  выбора при новых поручениях. Stable peer identity отличает заново созданное
  подключение с тем же именем; message IDs разных операций не конфликтуют.
  Также проверяются промежуточный ответ до завершения, обновление, отсутствие
  дубликатов/reasoning/private review, сохранение при временной ошибке, независимую
  прокрутку, Escape/focus return и мобильный экран. Ширина панели меняется
  перетаскиванием и клавиатурой в проверяемых границах, сохраняется при смене
  контрагента/закрытии и корректируется при изменении окна. Разделитель недоступен
  на mobile; основной чат остаётся читаемым без горизонтального переполнения.
  Owner API имеет pagination,
  company/chat/root-child isolation и пассивные reads; observation heartbeat
  даёт 15-секундный ceiling при более частых ранних фазах, TTL 45 секунд и не
  закрывает интерес другой вкладки. PostgreSQL restart сохраняет сообщения,
  pinned peer credentials и deadline без повторного Send. Закрытие не отменяет
  операцию; terminal/timeout не вызывает новые remote Get. Quarantine/отказ и
  integrity ошибки не открывают файлы; published файлы используют прежний
  authenticated download. Исходящие selected файлы отображаются отдельно от
  полученных, скачиваются из frozen operation snapshots, без unrelated файлов,
  blob refs или private путей. Недоступная копия/неподтверждённая отправка явно
  обозначаются. Legacy remote jobs не выдумывают прошлую переписку.

- ENT-AC-88: public `input-required` без owner marker сохраняет question identity,
  backend input ID и delivery до уведомления. Sleeping wait просыпается с прежним
  handle и non-terminal question; busy model/tool не прерывается, delivery идёт
  на safe boundary. Повторный snapshot/restart не создаёт второй вопрос; новая
  identity с тем же текстом доставляется отдельно. Guardrails предшествует модели;
  private/reasoning/пустой вопрос и auth wait не становятся question/credentials.
- ENT-AC-89: backend строит `core_agent_wait` v1 kind owner только из реального
  owner input/HITL/guardrails. Get/List, streams, push и recovery legacy waits
  показывают один safe marker без private вопросов/ответов/arguments/IDs.
  Получающий агент продолжает ждать и не разрешает foreign HITL/auth. Внешний
  follow-up не закрывает owner question/approval; targeted HTTP proofs проверяют
  отсутствие private content во всех публичных проекциях.
- ENT-AC-90: reply `core_agent_send_message` требует пару local `task_id`/`input_id`,
  проверяет scope/peer/current input/state/policy, отдельный HITL и frozen files.
  Half-pair, foreign/stale/wrong-peer/terminal/cancel/expired ответы не отправляются.
  JSONRPC и HTTP+JSON используют первоначальные remote IDs/pinned credentials,
  прежний local handle/deadline/send age/budgets. Durable queued и confirmed
  различаются; replay admitted call не создаёт второй Send, unknown требует
  reconciliation. Real PostgreSQL restart до/после question delivery, reply
  admission/intent/confirmation и private owner wait не повторяет мутацию либо
  nested Python prefix/remainder. Contracts v1/v2, checkpoint v1 и новые v3/v2
  совместимы; неизвестная/повреждённая версия fail closed.
- ENT-AC-91: text-only, JSON-only, mixed/empty-file и multiple typed Artifacts
  сохраняют artifact ID/name и ordered part identity в versioned manifest.
  Public text/data/file Parts становятся точными UTF-8/JSON/binary файлами под
  `a2a/<local-operation-id>/`; public Messages/questions остаются text. Structured
  immediate Message сохраняет Message origin без fabricated Artifact ID.
  Task get/list/wait, mailbox и summary содержат safe metadata/paths, без
  автоматического Artifact content; явное scoped чтение возвращает нужные bytes.
- ENT-AC-92: initiating inbound A2A Message attachments, follow-ups и standalone
  FileParts используют backend-owned `a2a/<local-batch-id>/`; UI upload остаётся
  в `attachments/`. Destination/имена не берутся из client/model remote path/ID.
  Inbound task attachments публикуются после admission/guardrails до model turn;
  remote result — после complete immutable early marker либо generic terminal success, без открытых partial chunks. Обе bindings, memory/PG,
  restart и legacy manifests/paths проверяют исходную chat/owner/source/root
  привязку, nofollow/collision safety и отсутствие скрытого перемещения старых files.
- ENT-AC-93: aggregate attachment limit учитывает binary/text/JSON/manifest bytes,
  encoded ceiling, bounded count/depth. Oversized/invalid/unsupported/URL Parts
  дают явную ошибку без partial publication/тихой потери. Whole-batch guards,
  claim/deadline/cancel и later exact file/text denials закрывают rejected/private
  content в tool results, workspace, peer panel и scoped preview/download;
  dedicated owner review сохраняет свои permissions. Recovery использует один
  принятый batch и не materialize-ит/передаёт отклонённый материал повторно.
- ENT-AC-94: real peer/native owner UI доказывают public question → guarded reply
  либо private `core_ask_owner` → ожидание той же Task → typed Artifact cards.
  Внутренняя owner переписка не видна peer; messages и file cards не смешиваются.
  Text/JSON file открывается existing scoped preview/download, новая отправка
  выбирает его только явно. Cleanup удаляет выбранные `a2a/` files и показывает
  receipts недоступными без recovery resurrection; unrelated UI attachments
  сохраняются. Adaptive polling/visible-panel cap, ordering/dedup, selected-peer
  timeline, keyboard/focus и current negative-material visibility сохраняются.

## Messages и явные Artifacts output contract v2

Следующие критерии — обязательства нового контракта, а не утверждение
implemented. Automated proofs MUST входить в обычные applicable gates до
обновления implementation-status; legacy proofs не заменяют v2 cases.

- [ ] ENT-AC-95: новая root Task закрепляет output contract v2 до dispatch;
  children наследуют parent snapshot version, accepted version-less root и его
  новые children/continuations остаются v1, включая catalog rendering/recovery.
  Ordinary tools-free final text становится одним durable `ROLE_AGENT` Message
  в `Task.status.message` и history; `Task.artifacts` пуст без explicit selection.
  Server-owned Message/Task/context IDs, Parts и completion metadata совпадают
  в live/Get/List/Subscribe/push и после PostgreSQL restart; `historyLength=0`
  убирает history, сохраняя final status Message. Detached stream не отменяет Task.
- [ ] ENT-AC-96: `core_message_send({text})` публикует public Message до receipt
  и оставляет Task working. Crash до/после commit и accepted-call replay дают
  соответственно ноль/один history Message и один charge, без второго ID.
  Два distinct calls с одинаковым текстом дают два сообщения. Transient previews,
  superseded token generations и repeated polling не становятся duplicate replies.
- [ ] ENT-AC-97: `core_response_artifacts` выбирает text/data-object/file Parts
  в заданном порядке, с optional bounded name и valid text media type. File bytes
  snapshot-ятся до receipt; workspace rewrite/delete не меняет опубликованный
  content/digest. Nonobject data root, NaN/infinity, malformed/unknown fields,
  URLs/traversal/symlinks/hardlinks/special files и foreign scope отклоняются
  до публикации всего нового batch; разрешённые mixed и empty-file batches проходят.
- [ ] ENT-AC-98: пока Task working, approved explicit batch появляется в GetTask,
  stream и UI немедленно, со stable server IDs и exact
  `core_agent_artifact: {version:1,complete:true,immutable:true}` metadata.
  Второй call append-ит новый batch; replace/remove/clear published невозможно.
  Replay одного call не удваивает IDs, byte/count totals, notifications или charge.
  Failure/cancel/budget-partial сохраняют ранее published Artifacts, без нового
  uncommitted/rejected batch. Final text не копируется в Artifact автоматически.
- [ ] ENT-AC-99: первый успешный batch pin-ит company attachment limit. Сумма всех
  Task Artifact UTF-8 text/canonical JSON/raw file bytes проверяется cumulatively;
  late company-limit increase не расширяет pin, decrease не повреждает принятый
  output. Границы 64 Artifacts/256 Parts и independent single-blob
  `MAX_RESPONSE_SIZE` проверяются до commit; ошибка одной части не даёт partial
  нового batch и сохраняет прежний набор/source files. Повтор не удваивает totals.
- [ ] ENT-AC-100: v2 `core_response_files` с непустым paths append-ит один explicit
  file-only Artifact немедленно, `[]` no-op. Для accepted legacy/version-less run
  сохраняются replace/clear-at-final semantics и прежний Artifact result.
  Upgrade/recovery не превращает старый результат в Message, не реконструирует
  selection из workspace/Markdown и не меняет pin. Неизвестный version fail closed.
- [ ] ENT-AC-101: real peer до terminal присылает complete immutable typed Artifact.
  Обе bindings проходят private quarantine, current scope/claim/deadline/cancel,
  whole-batch integrity и guardrails до доступных files/paths/metadata публикации.
  Partial/missing/unknown marker, append delta, lastChunk alone, rejected/expired
  content не открывают bytes. Generic third-party terminal Artifact/Message без
  marker по-прежнему принимается. Duplicate snapshots/restart не materialize-ят
  содержимое повторно; изменённый immutable ID не заменяет accepted batch.
- [ ] ENT-AC-102: Artifact text/data не склеивается с Message и не вставляется
  автоматически в tool result/mailbox/summary/model context. Модель получает safe
  typed manifest/paths и явно читает разрешённый файл; current negative review
  скрывает content во всех проекциях. Final Message v2 остаётся доступен после
  polling/reconnect/recovery независимо от history. Peer panel/UI различают
  chronological replies и отдельные immutable Artifact cards.
- [ ] ENT-AC-103: public working Message не создаёт input-required wait. Existing
  peer public question/reply LONG-05 работает в прежней Task/context; нового
  ask-caller tool нет. Private core_ask_owner question/answer/IDs и hidden
  reasoning не появляются в Messages/Artifacts/Get/history/streams/push/files
  для external caller; bearer/scope/privacy gates сохраняются.
- [ ] ENT-AC-104: controlled model + real PostgreSQL/peer/browser выполняют
  последовательность progress Message → mixed Artifact publish → дальнейший
  model/tool turn → final Message. Удаление/rewrite исходного файла, disconnect,
  restart и terminal success/failure/cancel не теряют ранее published identity/
  content; child capability scope не создаёт root attachment автоматически.
  Policy deny, HITL/refusal/timeout, guardrail refusal, malformed arguments и
  exhausted tool budget предотвращают новую публикацию; existing accepted output
  сохраняется. Local/shared usage и honest partial completion остаются canonical.

## Диагностика guardrails и sandbox

- [ ] ENT-AC-105: memory и PostgreSQL сохраняют committed classification/wait
  events атомарно, включая clear без workflow-version bump. Rollback убирает
  событие и решение; повторное finish/resolve/recovery не создаёт нового durable
  event. Phoenix spans и logs показывают finite verdict/reason/source, два
  разных deadlines, ожидание/решение/применение с stable IDs. Exporter outage
  не влияет на Task и оставляет bounded private delivery для следующей попытки;
  foreign-company rows, material payload/digest, paths, аргументы, owner answer
  и raw provider errors закрыты даже при включённом content capture. Diagnostic
  outbox не попадает в public A2A/push/model проекции; schema upgrades не нужны.
- [ ] ENT-AC-106: ошибки native check, очереди, supervisor/namespace/network,
  target exec и cleanup дают безопасные operator stage/reason/time/errno/exit
  attributes и отдельный cleanup outcome. Malformed/private frames и отсутствие
  resource counters не раскрывают текст/paths и не ломают execution. Wrapping
  terminal/Python сохраняет private диагностику и прежний public payload.
  Фактический pre-exec refusal остаётся ExecutionNotStarted; после release с
  неизвестным исходом остаётся SIDE_EFFECT_UNKNOWN, неподтверждённая смерть
  оставляет launcher unhealthy/quarantined. Native amd64/arm64 Linux gate
  проверяет реальные terminal/Python/background execution и cleanup; новые
  диагностические поля не служат основанием для retry или ослабления sandbox.

- EXEC-AC-01: в поставляемом Docker-образе `git --version` успешно выполняется
  внутри sandbox. Отсутствующая команда даёт `COMMAND_NOT_FOUND`; существующий
  файл с отсутствующим interpreter, ошибкой формата или прав — `COMMAND_EXEC_FAILED`.
  Отказ подтверждён до запуска, cleanup завершён; shell сохраняет собственный
  exit code/output. Ошибка инфраструктуры и неподтверждённый cleanup не маскируются
  под отсутствие команды.
- EXEC-AC-02: `cat > /dev/null; echo finished` завершается при обычном terminal
  call без ожидания ввода; pipe и перенаправление файла работают. Процесс без
  timeout ограничен 120 секундами (либо меньшим deployment cap), explicit timeout
  не превышает `CORE_AGENT_TERMINAL_MAX_SECONDS`; background и nested tool call
  соблюдают те же правила. Deadline действует без wait и не продлевается поздним
  wait; timeout подтверждает остановку потомков и возвращает `TOOL_TIMED_OUT`.
- EXEC-AC-03: terminal и Python принимают `cwd: /workspace/project` и
  `cwd: project` как один каталог. Выход за корень, похожий префикс и ссылка
  наружу отклоняются. Файл в workspace переживает следующий sandbox-вызов,
  файл в `/tmp` — нет. Описания инструментов сообщают эти свойства до вызова;
  Node.js/npm не добавляются в образ.
- EXEC-AC-04: same-slot restart с тем же boot ID и новым PID 1 подтверждает
  остановку точной execution generation. Unknown mutation завершается ABORTED
  без повторного model/tool call; terminal barrier проверяет дочерние generations.
  Другая replica, boot ID, незавершённый старый запуск, отсутствующая identity
  или stale receipt сохраняют барьер. Operator confirmation привязан к company,
  owner, run, instance, worker и generation; evidence сохраняется один раз.
  UI показывает ожидание подтверждения остановки без typing animation; после
  terminal reconciliation удаление чата снова доступно, история/файлы сохранены.

## Контекст исходящей A2A-переписки

- LONG-AC-06: два последовательных поручения из одного чата одному peer
  (включая разные root runs и сабагента) передают одинаковый полученный
  contextId, разные messageId и не переносят старый taskId. Task и прямой
  Message сохраняют context; input-required reply сохраняет task/context.
  Параллельные первые обращения сериализуются в memory и PostgreSQL, restart
  не теряет связь. Metadata-only edit сохраняет её, смена endpoint/credentials,
  peer, tenant, owner или чата изолирует её. Legacy checkpoint может безопасно
  продолжить существующий context. Lost claim/CAS, rollback file admission и
  late response не меняют связь; unknown Send/timeout не повторяется и не
  оставляет очередь бесконечно активной. Реальный HTTP peer получает context
  во втором Send; публичные проекции не раскрывают секреты.

## Строгий формат суммаризации

- CTX-AC-PROVIDER-DIAGNOSTICS: настоящий summary adapter различает в приватном
  `context.compaction.failed` HTTP отказ, timeout, transport failure, неверный
  ответ и отсутствие public text; сохраняет только проверенный status и конечные
  enum metadata. Проверяются OpenAI-compatible и Anthropic ответы, отказ при
  чтении body, reasoning-only response и поддельная metadata custom adapter.
  Raw provider content/credentials отсутствуют в событиях и публичной ошибке;
  прежние коды, число попыток, strict schema и terminal semantics сохраняются.

- STRUCTURED-REASONING: owner UI/API сохраняет nullable политику `auto/off/inherit`
  и finite compatibility через CAS; проверены default/reset, старые revisions,
  malformed input, tenant/owner isolation и недоступный каталог моделей.
  Реальные wire payload guardrails/summary/NER используют общую политику после
  объединения provider options, не меняют strict schema или обычные tool calls.
  Root/child/background и восстановление сохраняют admitted политику после
  изменения settings; legacy workflow наследует прежнее поведение. Пять detector
  slots остаются общими при разных policies. Исправленная NER policy не наследует
  permanent failure прежней. UI/Storybook отражают сохранение, конфликт и сброс;
  поля и подписи страницы настроек имеют согласованные отступы.

- CTX-AC-STRICT: фактический HTTP payload summary содержит собственную строгую
  схему в OpenAI-compatible и Anthropic форматах; унаследованная сторонняя схема
  заменена, основные model settings и tool schemas неизменны. Проверка охватывает
  обе формы OpenAI token cap, сохранение Anthropic effort, budget с учётом схемы
  и отказ до dispatch при недостатке окна. Ответ неправильной структуры или с
  неизвестными sources по-прежнему отклоняется локально. Provider rejection не
  ослабляет формат в повторной попытке; исчерпание попыток сохраняет штатную
  terminal recovery семантику.

## Область проверок prompt injection

- GUARD-AC-01: новые direct и Python-broker tool calls не отправляют сформированные
  моделью аргументы классификатору, в том числе `{}` и текст с инструкциями;
  malformed arguments, deny, HITL и budgets по-прежнему останавливают dispatch.
  При включённых guardrails результат инструмента проверяется отдельно.
  Legacy argument review восстанавливается с прежними attempt counters/deadline:
  checking продолжает существующую проверку, pending ждёт решения владельца.
  Новых reviews и автоматического одобрения нет; прежние отказы и timeout
  не обходятся через новый call или recovery.
- GUARD-AC-02: owner UI/API сохраняет `guardrails_enabled` через CAS, отклоняет
  не-boolean значения и недоверенных callers; omission сохраняет значение,
  отсутствие в legacy revision/snapshot означает `true`. Новые root/cron задачи
  закрепляют его; child/background/recovery сохраняют после изменения settings.
  При `false` новые входящие сообщения/файлы, tool/A2A results не вызывают
  классификатор и не создают guardrail wait. При `true` проверки сохраняются.
  Переключение не снимает существующие pending/negative material decisions,
  права, schema validation, tool policy/HITL, file integrity и sandbox limits.
  UI поясняет применение к новым задачам и сохраняет только изменённую секцию.
