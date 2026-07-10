# Spec-driven development

## Источник истины

Папка `spec/` задаёт ожидаемое поведение продукта. Код, tests и API schemas реализуют спецификацию, но не заменяют её. Если фактическое поведение отличается, до релиза меняется либо реализация, либо спецификация с явным продуктовым решением.

## Порядок изменения

Любая новая возможность или изменение семантики проходит один поток:

1. **Problem:** описать пользовательскую проблему и наблюдаемый результат.
2. **Target behavior:** изменить основной документ целевой спецификации без оглядки на текущий release.
3. **Product decision:** зафиксировать trade-off там, где возможны разные семантики.
4. **Acceptance:** добавить проверяемый критерий или сквозной сценарий.
5. **Release profile:** назначить поставку, в которой поведение становится обязательным.
6. **Implementation:** изменить код и tests со ссылками на конкретные headings/criteria.
7. **Verification:** доказать соответствие contract, integration, failure-mode и, при необходимости, eval tests.

Код MAY появиться как throwaway prototype до принятия решения, но не считается product implementation и не определяет публичную семантику задним числом.

## Статусы изменения

- `proposed` — обсуждается, не является обязательством release;
- `accepted` — принято в target spec;
- `scheduled` — включено в release profile;
- `implemented` — acceptance criteria проходят;
- `deprecated` — поддерживается на период миграции;
- `removed` — отсутствует в текущей major contract version.

Основные документы описывают accepted target. Release profile содержит только scheduled/implemented scope конкретной поставки.

## Структура spec change

Один логический change SHOULD быть одним reviewable commit и включать:

```text
Почему: какая проблема или риск обнаружены
Решение: какое наблюдаемое поведение выбрано
Альтернативы: только реально рассматривавшиеся варианты
Совместимость: API, persisted state, security и migration effect
Проверка: какие acceptance criteria доказывают решение
Поставка: какой release profile меняется
```

Отдельный ADR не нужен для каждого решения. Он оправдан, только если решение влияет минимум на две подсистемы, трудно обратимо или требует сохранения отклонённых альтернатив. ADR хранится в `spec/decisions/NNNN-short-name.md` и ссылки на него добавляются в затронутые документы.

## Traceability

- Implementation PR с изменением поведения MUST ссылаться на spec heading и acceptance item.
- Acceptance test SHOULD именовать проверяемое требование понятным стабильным названием.
- Release profile MUST ссылаться на target documents, а не копировать их полную семантику; допустимо копировать только checklist конкретного среза.
- Generated OpenAPI/JSON Schema MUST проверяться из одного источника типов и примерами из [публичного контракта](public-contract.md).
- Known deviation не прячется в issue: она либо блокирует release, либо временно записывается в его profile с owner и removal condition.

## Проверки документации

CI для `spec/` MUST проверять:

- относительные Markdown links;
- уникальность заголовков внутри файла;
- синтаксис JSON/YAML examples;
- отсутствие битых ссылок на error/event/tool names;
- соответствие public examples schema версии;
- что каждый release profile имеет scope, exclusions, acceptance и exit criteria.

Spellcheck и style lint MAY добавляться, если не создают шум вокруг смешанной русско-английской терминологии.

## Версионирование и миграции

- Target spec развивается вперёд; release profiles являются историческими snapshots и после релиза меняются только для errata.
- Backward-compatible public change сохраняет major version.
- Breaking change требует новой contract version, migration plan и периода совместимости, если безопасность не требует немедленного отключения.
- Persisted event/checkpoint/memory schema versioned независимо от transport contract и имеет deterministic migrations.
- Security fix MAY опередить обычный цикл, но нормативное изменение и regression criterion добавляются в том же release commit.

## Commit discipline

- Spec и связанный release profile коммитятся до реализации либо вместе с первым implementation slice.
- Коммит не смешивает несвязанные продуктовые решения.
- Формулировка commit message описывает принятое поведение, а не факт редактирования Markdown.
- Merge запрещён, если ссылки, examples или обязательные acceptance checks невалидны.
