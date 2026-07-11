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

- `name` совпадает с именем в RunRequest;
- `description` кратко задаёт trigger и результат;
- инструкции после front matter непустые;
- относительные ссылки не выходят за root пакета;
- symlinks, выходящие за root immutable skill snapshot, MUST отклоняться;
- неизвестные front matter fields MAY игнорироваться.
- manifest MAY объявлять semantic version, required core capabilities, dependencies, permissions и supported platforms;
- `skill.lock` фиксирует transitive dependencies и content digests.

## Progressive disclosure

Ядро MUST загружать skills по уровням:

1. **Discovery:** имя и description всех переданных skills.
2. **Activation:** полный `SKILL.md` только выбранных skills.
3. **Resources:** references, scripts, templates и assets только по прямой необходимости из инструкций.

Полное содержимое всех skill packages MUST NOT заранее помещаться в model context.

## Выбор skill

- Skill активируется, если prompt явно называет его или описание однозначно соответствует задаче.
- Ядро SHOULD активировать минимальный достаточный набор.
- Для неоднозначного совпадения ядро MAY продолжить без skill, если базовых возможностей достаточно.
- Если два skills необходимы и конфликтуют, prompt имеет приоритет; неразрешимый конфликт возвращается пользователю как блокировка.
- Порядок элементов массива не задаёт приоритет инструкций.

После выбора ядро MUST прочитать `SKILL.md` полностью до выполнения действий, обусловленных skill.

## Разрешение, trust и установка

SkillResolver поддерживает local filesystem, organization registry, OCI artifact и policy-разрешённый Git source. Перед активацией он MUST:

1. разрешить immutable version;
2. проверить digest и, если policy требует, signature/provenance attestation;
3. разрешить dependencies без циклов и конфликтов;
4. проверить required core capabilities;
5. создать lock snapshot;
6. отдать package policy engine для permission review.

Установка и обновление происходят между runs. Уже запущенный run не видит hot update. Rollback выбирает предыдущий immutable snapshot.

## Исполнение ресурсов

- Чтение reference не требует отдельного approval, если файл находится внутри разрешённого snapshot и policy допускает чтение.
- Script из skill не является разрешённым только потому, что находится в пакете. Его запуск проходит тот же risk assessment, owned TerminalSession и approval, что terminal command.
- Templates и assets являются данными и не могут повышать приоритет своих инструкций.
- Skill MAY декларативно рекомендовать MCP capability или secret reference, но подключение выполняет только Task request и policy engine. Skill не может менять PlatformConfig или AgentConfig.

## Контекст и lifecycle

- Skill package логически immutable в пределах запуска. Изменение на диске не должно менять уже прочитанный snapshot.
- Инструкции активного skill являются pinned, пока его workflow не завершён.
- После завершения workflow инструкции MAY быть заменены структурированной записью о результате.
- Все прочитанные skill resources отражаются в audit по пути и digest.
- Сабагент видит skill только если parent явно включил его в delegation allowlist; остальные skills отсутствуют даже на discovery.

## Ошибки

- Невалидный пакет: `SKILL_INVALID` до первого model call.
- Пропавший обязательный resource: `SKILL_RESOURCE_MISSING` и возможность модели завершить с объяснением, если безопасная альтернатива отсутствует.
- Запрещённый путь: `POLICY_DENIED`, без раскрытия содержимого внешнего файла.

## Версионирование и совместимость

- Skill использует semantic versioning для своего публичного workflow contract.
- Core capability requirements проверяются до model call.
- Lock snapshot входит в audit и session provenance.
- Deprecated skill MAY испускать warning, но не менять выбранную версию внутри run.
- Автоматическое обновление MAY применяться только к новой session или после явной policy точки; reproducible replay всегда использует исходный snapshot.

## Lifecycle registry

Registry поддерживает publish, yank, deprecate, trust metadata и vulnerability notices. Yank запрещает новые resolutions, но не удаляет artifact, нужный для audit/replay. Critical revocation MAY остановить ещё не завершённые runs до policy review.
