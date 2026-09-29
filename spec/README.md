# Core Agent Specification

Статус: нормативная target specification; enterprise v1 release candidate, реализация отслеживается отдельно

Назначение: нормативная спецификация целевого продукта и профилей его поставки.

## Идея продукта

Core Agent превращает пользовательский `prompt` в законченный агентный запуск. MCP-серверы и skills принадлежат конфигурации развёртывания и не передаются запросом. Клиент не собирает собственный цикл модели, не управляет контекстом и не реализует исполнение инструментов.

Ядро внутри предоставляет:

- reasoning-цикл модели;
- встроенные инструменты для терминала и изменения файлов;
- подключение MCP-серверов;
- обнаружение и применение skills;
- встроенную долговременную память с Markdown, графом, NER и hybrid retrieval;
- сжатие рабочего контекста при достижении 90% до 10–15%;
- фоновые задачи и неблокирующие сабагенты;
- общий owner UI, Keycloak, caller scope, постоянные папки чатов и Bubblewrap/egress в одном Kubernetes Pod;
- owner HITL, guardrails, cron и durable remote/time ожидания;
- A2A-интерфейс и OpenTelemetry observability.

## Как читать спецификацию

Начните с продукта и публичного контракта. Остальные документы нужны по мере реализации конкретной подсистемы.

1. [Продукт и границы](product.md) — пользователь, обещание и принципы итогового продукта.
2. [Архитектура](architecture.md) — подсистемы, состояния, durability и расширяемость.
3. [Конфигурация агента](agent-configuration.md) — feature switches, tools, MCP, memory mode и effective capabilities.
4. [A2A protocol](a2a-protocol.md) — основной внешний transport, Task lifecycle и notifications.
5. [Публичный контракт](public-contract.md) — отображение `prompt` на A2A.
6. [Kernel instructions](kernel-instructions.md) — защищённые правила ядра и порядок инструкций.
7. [Runtime и reasoning](runtime.md) — agent loop, model routing и восстановление.
8. [Фоновые задачи и делегирование](tasks-and-delegation.md) — async tasks, ожидание и сабагенты.
9. [Память агента](memory-service.md) — Markdown, graph/NER, hybrid search и file lifecycle.
10. [Контекст и суммаризация](context.md) — расчёт 90%, compaction до 10–15% и гарантии.
11. [Skills](skills.md) — формат, registry, выбор и progressive disclosure.
12. [Инструменты](tools.md) — built-ins и MCP.
13. [Файлы и transport artifacts](artifacts.md) — папки чатов, вложения, очистка, integrity и миграция прежних blobs.
14. [Local terminal sessions](execution-environment.md) — отдельные PTY/process groups/workspaces main и сабагентов в одном container.
15. [Безопасность и надёжность](security-and-reliability.md) — trust boundaries и ошибки.
16. [Наблюдаемость](observability.md) — OpenTelemetry, события, метрики, трассировка и evals.
17. [Критерии готовности продукта](acceptance.md) — сквозные свойства целевого ядра.
18. [Трассируемость реализации](implementation-status.md) — release gate от требования к доказательству.
19. [Spec-driven процесс](development-process.md) — как менять спецификацию и связывать её с реализацией.
20. [Профиль поставки v1](releases/v1.md) — текущий enterprise release candidate и его непроверенные обязательства.

## Нормативные слова

`MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT` и `MAY` трактуются как обязательное требование, запрет, рекомендация, нежелательное поведение и допустимая опция соответственно.

Если документы противоречат друг другу, приоритет имеют:

1. безопасность и защита данных;
2. публичный контракт;
3. критерии приёмки;
4. остальные разделы.

## Принятые продуктовые решения

- Каждый запуск принимает ровно одно поле: `prompt`. MCP и skills задаются конфигурацией развёртывания.
- Модель, credentials, S3/local workspace profiles, terminal policy и лимиты принадлежат platform config и не являются входами запуска.
- Session identity, вложения и control-команды передаются transport-ом вне тела запуска, поэтому не размывают однополевой контракт.
- Ядро поддерживает stateless runs, долгоживущие sessions, durable recovery и управляемую долговременную память.
- Инструменты исполняются последовательно, пока runtime не доказал независимость; разрешённый параллелизм остаётся внутренней оптимизацией.
- Делегирование дочерним агентам является внутренней возможностью и не добавляет полей клиенту.
- Основной внешний протокол — A2A; внутренний run и фоновая работа отображаются на A2A Task.
- Non-terminal A2A Task принимает follow-up Messages по своему `taskId`; runtime durable доставляет их model loop на safe boundaries без interrupt текущей операции или изменения EffectiveConfig.
- Memory является подсистемой Core Agent, а не отдельным MCP service.
- AgentConfig может отключить memory, built-in tools, отдельные MCP tools, skills, delegation и другие optional capabilities.
- Main agent и сабагенты используют общую память только когда parent явно делегировал им `core_memory_*` tools.
- Сабагент получает явный allowlist рабочих tools и skills; обязательные kernel tools нельзя убрать.
- Команды main и сабагентов имеют owned TerminalSessions; доступ к файлам/процессам/сети ограничен обязательным Bubblewrap в одном Kubernetes Pod, проверяемым на целевом кластере.
- Kernel rules для включённых capabilities имеют приоритет над настраиваемым agent prompt; AgentConfig определяет, какие capabilities вообще существуют.
- Ядро не раскрывает скрытую chain-of-thought; клиент получает ответы, статусы и краткие основания решений.
- Полный транскрипт хранится вне активного контекста, поэтому compaction не уничтожает аудит.

## Целевая спецификация и release profiles

Основные документы описывают итоговое поведение без привязки к очередности разработки. Они отвечают на вопрос «что представляет собой законченный продукт».

`releases/` содержит cumulative profiles: какие части целевой спецификации обязательны для конкретной поставки. Release profile MAY временно сузить поддерживаемые transports или режимы, но MUST NOT менять семантику уже реализованного публичного контракта.

## Управление изменениями

Спецификация версионируется вместе с кодом. Изменение нормативного поведения MUST одновременно обновлять:

- затронутый документ;
- [критерии приёмки](acceptance.md);
- версию контракта, если меняется внешний интерфейс;
- затронутые release profiles.

Новая возможность сначала описывается в целевой спецификации, затем назначается release profile. Архитектурное решение не должно приниматься только ради удобства первой версии, если оно закрывает путь к целевому поведению.
