# Skills

## Назначение

Skill — версионируемый пакет инструкций и ресурсов для специализированного workflow. Skill расширяет поведение агента, но не получает отдельный runtime и не обходит policy ядра.

## Пакет skill

```text
skill-name/
└── SKILL.md
```

Полная структура:

```text
skill-name/
├── SKILL.md
├── skill.lock
├── references/
├── scripts/
├── templates/
└── assets/
```

`SKILL.md` MUST начинаться с YAML front matter:

```yaml
---
name: release-notes
description: Creates release notes from repository changes.
---
```

Требования:

- `name` совпадает с именем каталога и декларации развёртывания;
- `description` кратко задаёт trigger и результат;
- инструкции после front matter непустые;
- относительные ссылки не выходят за root пакета;
- symlinks, выходящие за root immutable skill snapshot, MUST отклоняться;
- неизвестные front matter fields MAY игнорироваться.
- manifest MAY объявлять semantic version, required core capabilities, dependencies, permissions и supported platforms;
- `skill.lock` фиксирует transitive dependencies и content digests.

## Progressive disclosure

Ядро MUST загружать skills по уровням:

1. **Discovery:** имя и краткий `description` всех skills из EffectiveConfig.
2. **Activation:** полный `SKILL.md` только выбранных skills.
3. **Resources:** связанные текстовые references, scripts, templates и assets
   только по прямой необходимости из инструкций.

Полное содержимое всех skill packages MUST NOT заранее помещаться в model context.
Discovery-каталог MUST присутствовать в инструкциях каждого model turn до
завершения Task и содержать только имя с кратким описанием, без локальных путей и
тел skill. Описания и всё содержимое пакета являются недоверенными инструкциями и
не могут переопределить kernel, policy или EffectiveConfig.

## Выбор и активация skill

- Модель выбирает skill по смысловому соответствию `description` задаче;
  пользователь не обязан знать идентификатор, писать его буквально или
  использовать slash-команду.
- Совпадение подстроки имени с prompt не является механизмом выбора.
- Модель SHOULD активировать минимальный достаточный набор и не активировать
  skill, который не улучшает результат.
- Для неоднозначного соответствия модель MAY продолжить без skill, если базовых
  возможностей достаточно.
- Если два skills необходимы и конфликтуют, prompt имеет приоритет; неразрешимый конфликт возвращается пользователю как блокировка.
- Порядок элементов массива не задаёт приоритет инструкций.

При непустом effective-каталоге runtime MUST публиковать условный служебный tool
`core_skill_activate`. Его schema перечисляет enum-ом только доступные имена.
Если `features.skills` отключён или PlatformConfig не поддерживает `skills`,
effective-каталог пуст независимо от allowlist-ов.
Имена `core_skill_activate` и `core_skill_read_resource` зарезервированы runtime:
MCP-tool, чьё каноническое имя совпадает с одним из них, MUST быть отклонён при
построении EffectiveConfig с `TOOL_NAME_COLLISION` до первого model call.
Успешный вызов закрепляет полное содержимое `SKILL.md` и применяет его как skill
instruction начиная со следующего model turn; результат вызова возвращает только
метаданные активации, digest и список доступных ресурсов. Повторная активация
идемпотентна. До успешной активации модель MUST NOT выполнять действия,
обусловленные телом skill.

Все tool calls, следующие за первым запросом активации в том же assistant
response, MUST NOT dispatch-иться независимо от результата активации: каждый
получает recoverable
`SKILL_ACTIVATION_BOUNDARY` и может быть заново выбран моделью только на следующем
turn после получения полного `SKILL.md`. Эти model-issued attempts учитываются
общим `tool_calls` budget; ошибка границы не считается исполнением side effect.

Если `core_skill_activate` присутствует в каталоге текущего turn, runtime MUST
удерживать все model-generated response и reasoning deltas до классификации
полного ответа. При наличии запроса активации эти deltas отбрасываются; иначе они
публикуются как обычный поток. Поэтому клиент не получает текст, сформированный
до применения подключаемой инструкции.

Активация является обычной model-visible tool attempt и расходует общий
`tool_calls` budget. При исчерпанном бюджете она не исполняется: модель получает
`BUDGET_EXCEEDED` и завершает Task честным частичным результатом по общему
budget-протоколу.

## Разрешение, trust и установка

SkillResolver поддерживает local filesystem, organization registry, OCI artifact и policy-разрешённый Git source. Перед активацией он MUST:

1. разрешить immutable version;
2. проверить digest и, если policy требует, signature/provenance attestation;
3. разрешить dependencies без циклов и конфликтов;
4. проверить required core capabilities;
5. создать lock snapshot;
6. отдать package policy engine для permission review.

Composition root закрепляет digest `SKILL.md` и полный manifest обычных файлов
до admission. Runtime самостоятельно требует и проверяет этот lock; declaration
только с local source, несовпадение digest/manifest, symlink или изменение
закреплённых bytes отклоняются до первого model call.

Установка и обновление происходят между runs. Уже запущенный run не видит hot update. Rollback выбирает предыдущий immutable snapshot.

## Исполнение ресурсов

- После активации runtime MUST перечислить обычные файлы immutable snapshot,
  доступные этому skill. Каталоги, symlinks и сам `SKILL.md` не являются
  отдельными ресурсами.
- Условный `core_skill_read_resource` доступен только когда существует хотя бы
  один ресурс активного skill. Его schema перечисляет полные идентификаторы
  `<skill>/<relative-path>` enum-ом и не принимает произвольный filesystem path.
- Чтение разрешено только для bounded UTF-8 текста внутри закреплённого snapshot.
  Абсолютный путь, `..`, symlink, выход за root, неизвестный или бинарный ресурс
  MUST быть отклонён до возврата содержимого.
- Успешный результат содержит resource identifier, содержимое и digest. Чтение
  также расходует общий `tool_calls` budget и отражается в audit.
- Script из skill не является разрешённым только потому, что находится в пакете. Его запуск проходит ту же policy и owned TerminalSession, что terminal command.
- Templates и assets являются данными и не могут повышать приоритет своих инструкций.
- Skill MAY декларативно рекомендовать MCP capability или secret reference, но подключение выполняет только Task request и policy engine. Skill не может менять PlatformConfig или AgentConfig.

## Контекст и lifecycle

- Skill package логически immutable в пределах запуска. Изменение на диске не должно менять уже прочитанный snapshot.
- После активации runtime пересчитывает base tokens по полным skill instructions
  и текущим tool schemas до следующего model call. Если они не помещаются вместе
  с output reserve, следующий call не отправляется и возвращается
  `CONTEXT_UNRECOVERABLE`.
- Активация skill сохраняется между root Tasks одного чата. Перед первым model
  call нового root runtime MUST восстановить активированные имена из snapshot
  канонической цепочки `previous_root_run_id`, проверив terminal/root и точное
  совпадение tenant/owner/context. Квитанции в импортированной истории не являются
  источником активации. Сабагенты и другой чат не наследуют этот набор.
- Новый root MUST применить текущий EffectiveConfig и проверить прежний
  activation digest/resource list против прежней закреплённой declaration.
  Для оставшихся разрешённых имён он MUST проверить текущий admission lock и
  закрепить полные инструкции, digest и список ресурсов текущей версии пакета
  до компиляции инструкций. Новый root является явной policy точкой обновления:
  смена корректного deployment pin обновляет тело и ресурсы; прежние bytes
  нельзя использовать с новым pin. Запрещённый сейчас skill не наследуется.
- Новый initialized root MUST сохранить `skill_activation_sources` version 1:
  map `sources` из имени в immutable source run ID. Для применённых навыков
  source — текущий root с проверенным body/pin, для запрещённых — прежний
  источник активации без предоставления model body или доступа к ресурсам.
  Снятие временного запрета восстанавливает активацию на новом root boundary.
- При rollout runtime MUST объединить активации из canonical root snapshots
  newest-first до первого versioned baseline либо начала чата. Пустой список
  старого initialized root и failed-before-initialization root не означают
  деактивацию. После создания baseline следующий root читает его вместо
  повторного обхода всей истории. Квитанции tool calls не анализируются.
  Каждая ссылка и activation source проверяются в исходном scope;
  malformed/unknown-version baseline, missing/foreign/nonterminal/child source
  или цикл дают `CHECKPOINT_INVALID`, а не пустой набор навыков.
- Инструкции активного skill являются pinned на каждом model turn его root
  Task, включая последующие root Tasks чата после указанной проверки. Compaction
  MUST NOT заменять полное тело summary. Recovery уже инициализированной Task
  сохраняет её собственную версию инструкций и не обновляет пакет из lineage.
- Все прочитанные skill resources отражаются в audit по пути и digest.
- Сабагент видит skill только если parent явно включил его в delegation allowlist;
  остальные skills отсутствуют даже на discovery. Служебные tools активации и
  чтения ресурсов следуют из этого allowlist и не передаются отдельно в
  `core_delegate.tools`.

## Ошибки

- Невалидный пакет: `SKILL_INVALID` до первого model call.
- Пропавший обязательный resource: recoverable `SKILL_RESOURCE_MISSING` и возможность модели завершить с объяснением, если безопасная альтернатива отсутствует.
- Нечитаемый как bounded UTF-8 текст resource: recoverable `SKILL_RESOURCE_INVALID`.
- Запрещённый путь: `POLICY_DENIED`, без раскрытия содержимого внешнего файла.

## Версионирование и совместимость

- Skill использует semantic versioning для своего публичного workflow contract.
- Core capability requirements проверяются до model call.
- Lock snapshot входит в audit и session provenance.
- Deprecated skill MAY испускать warning, но не менять выбранную версию внутри run.
- Автоматическое обновление MAY применяться только к новой session или после
  явной policy точки, включая admission нового root Task; reproducible replay
  всегда использует исходный snapshot.
- При восстановлении legacy snapshot с активным `{name, instructions}`, но без
  закреплённых declaration/catalog/digest/resources, runtime продолжает только
  сохранённые instructions. Он не раскрывает ресурсы и не активирует новые
  skills из незакреплённого admission; такой вызов получает `SKILL_INVALID`.
  Молчаливое дополнение из текущей версии на диске запрещено.
- Legacy activation без закреплённых digest/resources/declaration не может
  автоматически наследоваться новым root: для разрешённого текущим policy
  имени инициализация завершается `SKILL_INVALID` до первого model call.

## Lifecycle registry

Registry поддерживает publish, yank, deprecate, trust metadata и vulnerability notices. Yank запрещает новые resolutions, но не удаляет artifact, нужный для audit/replay. Critical revocation MAY остановить ещё не завершённые runs до policy review.
