# Файлы чатов и transport artifacts

## Назначение и границы

Постоянные файлы принадлежат чату и хранятся под `CHAT_WORKSPACE_ROOT`; процессы видят только свой `/workspace`. В output contract v2 обычные replies являются Messages; A2A Artifact представляет только явно выбранный отдельный typed результат. Legacy accepted runs/results сохраняют прежний контракт. Transport blobs, bounded-output offload и immutable workspace snapshots сохраняют самостоятельное назначение, digest/integrity и provenance; они не являются удаляемым model-callable artifact service.

Сохранённый transport blob имеет непрозрачный ID, tenant/chat/caller scope, media type, фактический размер, SHA-256 digest и source/task provenance. При чтении проверяются доступ, размер и digest; повреждённый объект не выдаётся. Ссылки на скачивание проверяют права на каждом новом HTTP-запросе и не открывают общий filesystem сервера. Snapshots имеют проверяемый commit manifest и не восстанавливают вручную удалённые файлы поверх новой revision workspace.

В явном [Cloud.ru профиле](cloudru.md) локальные paths являются RAM cache,
а подтверждённые bytes всех workspace, quarantine, results и offload сохраняются
через S3 SDK по planned CLD-04 для production release. Первый диагностический
trial использует временную FS без гарантии сохранности этих bytes после замены Pod. Это transport/workspace durability и не возвращает
удалённый model-callable artifact service. Scope, integrity, guardrails и
publication barrier этого документа действуют также после восстановления.

## Вложения и управление файлами

### FILE-02. Приём вложений

- UI и A2A принимают файлы и сохраняют их в папке нужного чата.
- Новые A2A-вложения, включая FileParts исходного inbound Message и follow-up,
  публикуются под backend-owned `a2a/`; UI upload остаётся в `attachments/`.
  Явное Cloud.ru S3/Landlock исключение сохраняет private immutable original
  до admission, а writable UI-копию публикует в `inputs/<batch>/` по CLD-04.
  Прежние принятые batches сохраняют свои manifest/path без перемещения или
  реконструкции после upgrade. Подробности Artifact materialization — FILE-05.
- Подозрительный файл до решения guardrails изолируется от исполняющей среды;
  при отказе его содержимое не становится доступным агенту по GUARD-02.
- Лимит применяется к сумме размеров вложений одного сообщения.
- Значение по умолчанию — 25 МБ; лимит настраивается.
- Одна настройка лимита действует для входящих и исходящих вложений
  через UI и A2A; правила исходящей проверки описаны в FILE-03.
- Для однозначного API-контракта МБ означает 1 000 000 байт;
  исходное значение лимита — 25 000 000 байт.
- Для base64 сравнивается размер декодированных файлов; transport/proxy лимиты
  должны учитывать размер кодирования и остального сообщения.
- В контекст передаются фактические безопасные имена и путь к папке:
  «Прикреплённые файлы: …; папка: …». Данные файлов не подставляются целиком
  в system prompt.
- Содержимое, исходные имена файлов и сообщения внешнего агента недоверенные;
  их нельзя интерпретировать как privileged instructions.

До публикации проверяется каждый файл вместе с исходным и фактическим именем
и immutable metadata всего набора. Неподдерживаемое или неполное извлечение,
включая пустое содержимое, требует решения владельца как непроверенный материал.
Private review ссылается на конкретные batch/index и версию; владелец может
скачать исходные bytes через авторизованный guardrail endpoint без доступа
модели или sandbox к карантину.

Публикация файлов одного сообщения атомарна: весь набор остаётся в карантине,
пока каждый файл не получил clear/allow. Reject/timeout одного файла исключает
весь файловый набор сообщения; запрос решения явно содержит
`affected_scope=file_batch`. Отдельно проверенный текст сообщения сохраняется,
а модель получает безопасное уведомление об исключении набора без имён или
содержимого отклонённых файлов. Владелец может отправить допустимый набор новым
сообщением. Отказ по точным bytes файла сохраняется при изменении имени или
batch ID в том же чате. Уже опубликованный набор при recovery сверяется с
принятым manifest; несовпадение не устраняется повторной копией из старого snapshot.
В S3-режиме original проверяется отдельно от рабочей копии; повторная публикация
не отменяет правки/удаления уже опубликованных writable `inputs/` и `a2a/`.
Receipt первой публикации и workspace manifest фиксируются одной транзакцией
после сохранения bytes. Original download/review не читают изменяемую копию.

Пути должны исключать выход из папки и переход по ссылке к чужим данным.
При совпадении безопасного имени с существующим файлом сохраняются оба:
новое вложение получает свободное имя с суффиксом перед расширением,
например `report_2.pdf`, затем `report_3.pdf`. Существующий файл не заменяется.
Правило одинаково для UI и A2A, включая одинаковые имена в одном сообщении
и конкурентные загрузки. Выбор свободного имени и сохранение исключают
перезапись при гонке; совпадение с каталогом или ссылкой также не даёт права
перезаписать существующий объект или перейти по ссылке.

UI и результат приёма A2A-вложения отражают фактическое сохранённое имя;
агент получает это же имя и доступный ему путь в сообщении о вложениях.

Сообщение с вложениями принимается целиком. Ошибка загрузки, декодирования
или сохранения хотя бы одного файла либо превышение суммарного лимита
отклоняет всё сообщение. Ни текст, ни успешно загруженная часть вложений
не передаются агенту; новое выполнение по этому сообщению не начинается,
follow-up не доставляется в активную задачу. Уже работающая задача продолжает
выполнение без отклонённого сообщения. UI или A2A caller получает явный отказ
с причиной, позволяющей исправить запрос и повторить отправку.

HTTP body до SDK/protobuf ограничивается независимо от owner settings:
`A2A_MAX_REQUEST_BYTES` по умолчанию 40 000 000 encoded bytes. Проверяются реальные
stream chunks, а не только Content-Length; compressed request bodies не принимаются.
JSON с повторными keys и неканонический standard base64 отклоняются до admission.
При увеличении decoded company limit оператор MUST соразмерно увеличить этот
deployment ceiling для JSON/base64 overhead. Понижение company limit не меняет
ceiling и не мешает dedup уже принятого сообщения с неизменными bytes.

До готовности полного набора файлы находятся во временной области, недоступной
агенту. При отказе новые файлы этого сообщения не публикуются в workspace;
уже существующие данные сохраняются. Неуспешная загрузка и её временные файлы
не должны становиться принятыми после restart. Временные файлы отклонённой
загрузки удаляются сразу. Осиротевшие остатки после сбоя автоматически
очищаются в течение 24 часов; restart не начинает срок заново, а уже
просроченные остатки очищаются при восстановлении сервиса.
Очистка не затрагивает активные загрузки, принятые вложения и материалы,
ожидающие решения guardrails. Перед удалением проверяется актуальная связь
файлов с загрузкой или принятой задачей.

Новые upload manifests версии 1 сохраняют trusted tenant как дополнительное
поле; прежние manifests читаются без перезаписи принятых bytes. Автоматическая
очистка авторизованного deployment ограничена его company. Rowless manifest
без доказанного tenant или с чужим tenant сохраняется для reconciliation.
Каждый проход обхода каталогов ограничен по числу просмотренных entries;
последующие проходы продолжают cursor. Перед удалением проверяются актуальные
authoritative references всех компаний, независимо от scope очистки.

Это правило описывает технический приём сообщения. Последующее решение
guardrails по уже принятому материалу обрабатывается по GUARD-01–04:
отказ владельца исключает материал и позволяет продолжить задачу без него.

### FILE-03. Выдача и отправка

- Агент может передать результирующие файлы владельцу через UI.
- В обоих направлениях A2A файлы передаются средствами A2A, а не путём
  предоставления доступа к общему workspace сервера.
- Выдача каждого файла проверяет scope вызывающего.
- К исходящим вложениям в UI и A2A применяется тот же настраиваемый лимит,
  что и к входящим: по умолчанию 25 МБ суммарно на сообщение.
- До отправки проверяется сумма фактических размеров всех файлов сообщения,
  без увеличения на base64. Лимит не применяется отдельно к каждому файлу
  или кадру транспортного потока и не обходится ссылкой вместо содержимого.
- При превышении лимита сообщение с вложениями не отправляется частично:
  агент получает структурированную ошибку с допустимым и фактическим размером
  и может подготовить меньший набор файлов. Исходные файлы в workspace
  сохраняются; само превышение не завершает задачу автоматически как failed.
- A2A использует FileParts/Artifact parts по [Публичному контракту](public-contract.md);
  transport reference не открывает чужие данные.

### Typed explicit publication v2

Новый output contract закрепляется при admission; обычный final text не создаёт
Artifact. `core_response_artifacts` публикует immutable typed batch немедленно
после whole-batch validation/guardrails/HITL и durable commit, пока Task
остаётся `working`. Artifact/Part IDs, names, ordering, content, actual sizes,
digest и trusted tenant/chat/owner/Task/run lineage сохраняются в scoped
immutable manifest. Snapshot чтение файлов не даёт доступа к соседнему чату
либо platform credentials; paths не становятся публичными references.
После commit workspace mutation/removal не меняет опубликованный content.

Первая успешная публикация закрепляет Task-wide company
`attachment_limit_bytes`: суммарные UTF-8 text, canonical JSON object и raw file
bytes всех последующих Artifacts учитываются относительно этого pin, default
25 МБ. Task имеет не более 64 Artifacts и 256 ordered Parts.
`MAX_RESPONSE_SIZE` ограничивает один blob отдельно. Late company-limit change
не расширяет и не инвалидирует прежний Task pin. Empty files допустимы;
base64 overhead не увеличивает сумму actual content bytes.
Ошибка, corrupt snapshot или oversized batch не публикует его частично и
не повреждает прежние Artifacts.

Новый успешный call только append-ит полный batch. Уже published Artifact
не заменяется, не удаляется и не очищается tool-ом, finalization, failure,
cancel или последующим явным выбором. Retry/recovery accepted call использует
те же IDs/receipts; две разные calls не выводятся из совпадения текста или
bytes. V2 `core_response_files` — file-only compatibility alias: непустой
`paths` append-ит один Artifact немедленно, `[]` ничего не меняет.
Наличие файла, text reply, tool result или child result не является выбором.

GetTask, history/stream/push, UI и scoped download показывают один committed
immutable batch; public bytes не дублируются в обычном final Message.
Перед каждым новым download проверяются текущие права и целостность whole batch.
Earlier Artifacts остаются в failed/canceled Task; неapproved/uncommitted
batch после terminal outcome не публикуется. Immutable transport blobs
сохраняются независимо от retired named artifact CRUD service; новая
Сама typed publication не требует S3/Mongo инфраструктуры; выбор S3 для
Cloud.ru workspace durability задаётся отдельным CLD-04.

### Legacy file selection

Следующие pending selection/final-frame правила относятся к legacy accepted
runs/results. Они сохраняются без переформатирования при upgrade и recovery.

`core_response_files` сохраняет immutable snapshots выбранных workspace файлов
в transport storage. Manifest version 1 закрепляет trusted tenant/chat/owner,
Task/run и ordered references; file ID или digest не даёт права читать blob.
Один успешный tool receipt атомарно заменяет весь pending manifest. Сбой до commit
не меняет прежний набор. Отсутствие поля у прежних workflow/results означает
пустой набор; upgrade не восстанавливает выбор из Markdown или legacy artifacts.
Persisted `outgoing_files` сохраняется при обычном и budget-partial завершении,
не меняя completion provenance. Recovery читает frozen blobs, а не workspace;
изменение или удаление исходных файлов не меняет уже выбранный результат.

Перед первым final attachment frame проверяются **все** references, scope,
actual sizes/digests и общий лимит, закреплённый при успешном выборе этого набора.
Каждый новый выбор использует текущую company настройку; её позднее изменение
не инвалидирует уже принятый выбор и чтение завершённого результата.
Ошибка не публикует часть набора.
GetTask, live stream, passive subscription и recovery показывают одинаковые
ordered файлы и IDs; bytes не дублируются в terminal Message. Новое HTTP
скачивание проверяет текущие права и сохранённый manifest соответствующей Task.
Проверка scope обязательна и при одинаковых bytes в разных чатах одного tenant.
Deployment ceiling transport storage (`MAX_RESPONSE_SIZE`) остаётся независимым
ограничением одного blob. Если увеличенный company limit превышает его,
подготовка возвращает `ARTIFACT_TOO_LARGE` без изменения принятого набора;
увеличение company limit не расширяет platform storage ceilings автоматически.

Для новой задачи внешнему агенту `core_agent_send_message.files` выбирает отдельный
полный набор только для этого вызова. Он не изменяет final selection и не наследует
файлы workspace автоматически. Snapshots, их aggregate ceiling и trusted source
scope закрепляются до HITL; outbound raw Parts содержат safe basename/media type
и bytes, без локальных путей, private blob IDs и предоставления доступа к чату.
Общий outbound набор проверяется до Send intent, включая recovery. Файлы remote
result принимаются атомарно с fenced terminal operation outcome в quarantine
исходного чата; guardrails и publication barrier предшествуют видимости модели.
Внутренний child сохраняет свой source run, а public Task FK выводится через
проверенную root ancestry. Human-wait progress и failed/expired outcomes не
публикуют attachments. Полный контракт —в [Удалённых A2A-агентах](tasks-and-delegation.md).

### FILE-05. Typed A2A artifacts в workspace

Полученный Artifact сохраняет исходный разрешённый `artifactId`, name, порядок
артефактов и Parts и различие text/data/file; его текст не становится Message.
Каждая public text Part материализуется в точные UTF-8 bytes: `text/markdown`
получает `.md`, прочий текст `.txt`. Data Part — детерминированный UTF-8 JSON
с `allow_nan=False`, file Part — проверенные decoded bytes. Structured Part
immediate Message также сохраняется файлом с Message origin/identity, без
выдуманного Artifact ID. Public Messages, progress и вопросы остаются text.
Reasoning/private/replay и скрытая metadata не материализуются и не публикуются;
credential filtering и guardrails применяются до любой доступной проекции.

Remote results используют `/workspace/a2a/<local-operation-id>/` с backend-owned
`manifest.json` и отдельными artifact/part файлами. FileParts исходного inbound
A2A Message/follow-up используют `/workspace/a2a/<local-batch-id>/`. Directory
selection берётся только из authenticated transport/source; client/model не
передаёт destination. Remote IDs/names не становятся filesystem paths. Safe
names, collision handling, nofollow и whole-batch integrity сохраняются; не
перезаписывается чужой набор или существующий файл.

Browser UI также использует owner A2A transport. Его uploads сохраняют
`attachments/`, только если подтверждённый introspection `client_id` совпадает
с deployment `KEYCLOAK_UI_CLIENT_ID` и principal является владельцем.
Другие owner/external A2A-клиенты используют `a2a/`; Message metadata или
переданный заголовок не выбирают каталог. Отсутствующий/иной client ID не даёт
browser-origin semantics. Уже принятый manifest и повторный receipt неизменны.

Versioned manifest сохраняет artifact/Message origin, исходные разрешённые IDs
и name, ordered typed part entries, safe local path, media type, actual size и
SHA-256. Payload находится в отдельных файлах, не в model-callable artifact
service. Старые manifests и `attachments/` читаются по прежнему контракту;
новая форма не выдумывает typed identity для старых плоских результатов.

Text-only, JSON-only, mixed и empty-file результаты проходят тот же private
staging/admission/guardrails/atomic publication, что binary FileParts. Для
materialized remote result sum limit использует закреплённый
`attachment_limit_bytes`. Для marked immutable cumulative batches он считает
все принятые published payload bytes Task: binary bytes, UTF-8 text и canonical
JSON; exact repeated Artifact не расходует лимит повторно. Generated private
manifests считаются отдельно и остаются bounded через конечные Artifact/Part
counts, validated metadata и encoded transport ceiling; они не уменьшают
доступный payload limit. Generic legacy terminal intake сохраняет прежний
accounting: binary bytes, UTF-8 text, JSON **и generated manifest** входят в
aggregate limit. Encoded transport ceiling независим в обоих случаях.
Artifact/part counts и JSON depth конечны и bounded; oversized/invalid result
даёт явную ошибку до публикации всего набора, без silent part loss. URL FileParts
не скачиваются автоматически и отклоняются existing unsupported-file contract.

Generic third-party/legacy remote result публикуется после complete successful
terminal Task/Message и current claim/cancel/deadline/canonical source/root scope.
Для peer, использующего согласованный immutable early-publication contract,
отдельный Artifact MAY приниматься до terminal Task только с exact marker
`metadata.core_agent_artifact: {version: 1, complete: true, immutable: true}`.
Receiver MUST иметь весь ordered набор его typed Parts: incomplete chunk,
append-дельта, один `lastChunk`, отсутствие/неполная/неизвестная marker version
не разрешают early content publication. Generic terminal fallback остаётся
доступен без Core Agent marker; обычный A2A peer не обязан вводить extension.

Early Artifact проходит те же bounded whole-batch parsing, private quarantine,
current guards/deadline/claim/cancel/scope, material reviews и integrity до
доступности model paths/workspace/peer cards. Marker не даёт data/owner authority
и не разрешает пропуск review. Пока batch не approved/published, никакие text,
data, file bytes либо доступные paths не выдаются через Message, summary,
tool result, mailbox или preview/download. Identity/content одного immutable
Artifact закрепляется: exact repeated snapshot/recovery не создаёт копии,
поздняя append/replace mutation того же ID отклоняется без изменения accepted
content. После completion receiver сохраняет ранее accepted Artifacts; failed/
canceled peer не делает их исчезнувшими, но не открывает новые unpublished
chunks либо rejected материал. Current negative-material decisions сохраняются.
Inbound initiating attachments публикуются после их обычных admission/guardrails
до выполнения поручения, а не ждут terminal состояния нашей Task.
Refusal/timeout одной части закрывает соответствующий atomic incoming batch;
ранее независимо published Artifacts не переписываются. Later exact negative
decisions нельзя обойти alternate filename, receipt, preview/download или reread.
Обычный dedicated owner review сохраняет отдельные private permissions.

Model tool results, task get/list/wait и mailbox содержат status и safe artifact/
part metadata/paths, но не содержимое text/data Parts в `text`, summary или
служебной подсказке. Модель явно читает нужный файл через разрешённые tools;
повторный GetTask/restart не materialize-ит batch повторно и не возвращает
исключённые bytes в context. Owner UI использует existing scoped file open,
preview/download cards. Files участвуют в cleanup; удалённый/недоступный файл
не объявляется скачиваемым и не восстанавливается молча из старого receipt.
Incoming artifact files не выбираются для нового Send/final response автоматически.

### FILE-04. Очистка workspace через UI

Владелец может удалить старые файлы из workspace выбранного чата через UI.
До удаления отображается список файлов: имя, относительный путь, размер
и дата последнего изменения. Владелец отмечает файлы и перед подтверждением
видит точный перечень выбранного, количество и суммарный размер.
Просмотр списка сам по себе ничего не удаляет.

Owner preview API и скачивание текущего файла описаны в
[Публичном контракте](public-contract.md). Preview выводит только обычные файлы
с одной ссылкой, относительные пути и filesystem identity tokens; приватный
карантин, staging и служебные manifests публикации не отображаются. Чтение
не создаёт отсутствующую папку. Bounded traversal возвращает явную ошибку при
превышении лимита, а не неполный перечень под видом полного; для большого дерева
владелец может сузить каталог. Пагинация сохраняет server timestamp фильтра,
однако не замораживает файловую систему. После restart cursor preview требует
обновления списка; durable история чата имеет отдельный контракт cursor.

В UI доступен фильтр «не изменялись более N дней»: владелец задаёт N,
а список отбирается по времени последнего изменения файла. Возраст считается
от серверного времени формирования списка; день означает 24 часа. Файл
на точной границе N дней ещё не соответствует условию «более N дней».
Из найденного списка владелец вручную отмечает файлы для удаления;
по умолчанию ничего не выбрано. Фильтр не запускает автоматическую очистку.

Подтверждение относится только к показанному и выбранному перечню. Файлы,
появившиеся позднее, не попадают в удаление автоматически. Если выбранный файл
после просмотра изменился или был заменён, он пропускается с пояснением
и требует обновления списка перед новым решением владельца.
Сервер повторно проверяет роль владельца и принадлежность путей выбранному
workspace; выход за его границы, в том числе через символические ссылки,
не допускается.

После операции UI показывает, какие файлы удалены, какие пропущены и какие
не удалось удалить, с причиной для каждого неуспешного результата. Ошибка
части операции не должна отображаться как удаление всего выбранного.
Очистка файлов сохраняет сам чат, историю и записи задач; перезапуск
не должен возвращать удалённые файлы в workspace из старого снимка.

Подтверждённая selection сохраняется immutable intent до filesystem effects;
request_id обеспечивает повтор точного тела без новой операции. GET receipt
read-only и доступен всем владельцам company, включая после browser reload.
Private capture/journal и content digests обнаруживают изменение исходного
объекта, включая запись в тот же inode с сохранением size/mtime. Несоответствие
не разрешает удалить подмену. Captured object восстанавливается без перезаписи;
неоднозначный outcome сохраняет данные и barrier для reconciliation.
Принятый незавершённый intent блокирует новые roots до завершения, не блокируя
чтение файлов/receipt; duplicate ранее принятой Task не создаёт новую работу.
Проверенные deletions и revision переживают restart; старые preview tokens
не используются как автоматическое подтверждение обновлённого файла.

Пока в чате есть активная задача, просмотр и фильтрация файлов доступны,
а удаление через очистку workspace заблокировано. Это относится и к ожиданию
HITL, ответа владельца, внешнего агента или времени пробуждения. UI показывает
причину блокировки, сервер повторно проверяет состояние перед удалением.
Заблокированная очистка не ставится в очередь: после завершения задачи
владелец может повторить её вручную.

Проверка занятости и очистка согласованы с приёмом новых задач, включая cron:
конкурентный запуск не может привести к удалению файлов во время активной
задачи. Эти операции не должны пересекаться даже при устаревшем состоянии UI.

## CLEAN-01. Удаление dedicated artifact service

`core_artifact_save`, `core_artifact_load`, `core_artifact_list`, их registry, schemas, handlers и model instructions MUST быть удалены. Код, зависимости, S3/MongoDB adapters и configuration, используемые исключительно этими tools, также удаляются. Транспортные A2A Artifacts, storage для результатов/offload и workspace snapshots сохраняются; само слово artifact не является критерием удаления.

До удаления service все входящие вложения переводятся на chat workspace. Прежние `ARTIFACT_STORAGE_*`, dedicated `ARTIFACT_S3_*`/`ARTIFACT_MONGODB_*` и `RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS` перестают задавать runtime capabilities. Keycloak/legacy upgrade MUST диагностировать неподдерживаемые старые настройки;
явный диагностический Cloud.ru trial игнорирует их без включения backend по
[CLD trial](cloudru.md); отключение tools не разрешает молча потерять прежние файлы.

Согласованный cutover не имеет прежних named artifact данных для переноса.
Export/import utility, перенос S3/MongoDB blobs и новый import journal не входят
в CLEAN-01. Удаляются только код и конфигурация приложения; внешние S3 buckets,
MongoDB databases и Kubernetes resources не удаляются. История, chat workspace,
transport artifacts и snapshots сохраняются по своим прежним правилам.
