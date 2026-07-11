# Продукт и границы

## Пользователи

- Разработчик продукта встраивает agent core через A2A или совместимый embedded SDK.
- Оператор задаёт модели, policies, budgets, tenancy, data retention и инфраструктуру.
- Конечный пользователь формулирует задачи, наблюдает ход работы и принимает human-in-the-loop решения.
- Автор skill или MCP server расширяет возможности без изменения ядра.

## Обещание продукта

Пользователь передаёт задачу и доступные расширения. Core Agent автономно выполняет безопасные шаги, запрашивает решение человека только там, где политика требует его, и возвращает наблюдаемый результат.

Короткая формула:

```text
prompt + MCP + skills -> события выполнения + итоговый ответ
```

## Что означает «всё внутри»

Core Agent MUST владеть следующими обязанностями:

- построение системных инструкций;
- выбор режима reasoning и вызовы модели;
- ведение agent loop до результата или явной ошибки;
- учёт токенов, compaction и восстановление контекста;
- обнаружение, загрузка и применение skills;
- регистрация, валидация и исполнение встроенных и MCP-инструментов;
- оценка риска и approval flow;
- ограничение вывода инструментов;
- Markdown-memory, graph/NER indexing и hybrid retrieval;
- фоновые Tasks, passive wait и делегирование сабагентам;
- изолированный execution plane;
- A2A communication и OpenTelemetry observability;
- стриминг событий, аудит и отмена запуска.

Клиент MUST NOT быть обязан повторять эту логику. Полнота продукта измеряется не числом встроенных workflow, а тем, насколько надёжно ядро выполняет любой разрешённый workflow через единый контракт.

## Два уровня конфигурации

### DeploymentConfig

Настраивается оператором при старте процесса и не входит в запрос запуска:

- модель, provider и credentials;
- рабочая директория и допустимые filesystem roots;
- sandbox и сетевые ограничения;
- режим approval и правила риска;
- secret store;
- хранилище транскриптов и артефактов;
- тайм-ауты, денежные и вычислительные бюджеты;
- A2A bindings, Agent Card и push-notification policy;
- session, memory и checkpoint stores;
- model routing и fallback policy;
- tenant identity, quotas и data governance;
- registry и trust policy для skills/MCP.
- AgentProfilePrompt, который не может заменить KernelInstructions;
- ExecutionEnvironment backend/images и OTLP endpoints.

### RunRequest

Передаётся пользователем для конкретной задачи и содержит только:

- `prompt`;
- `mcp`;
- `skills`.

Это разделение MUST сохраняться во всех SDK и A2A adapters.

## Продуктовые режимы

### Stateless run

Одна задача без переноса состояния между вызовами. Ядро всё равно сохраняет audit и artifacts согласно policy.

### Stateful session

Последовательность запусков с общей историей, pinned facts, artifacts и разрешённой session memory. Session адресуется transport-ом; тело каждого запуска остаётся `prompt + mcp + skills`.

### Durable autonomous run

Долгая задача может ждать человека, переживать временные сбои и рестарт worker-а, восстанавливаясь из checkpoint без повтора неподтверждённых side effects.

### Embedded/local run

Control plane может быть встроен в приложение или работать локально, но terminal, skill scripts и stdio MCP всё равно исполняются в отдельном ExecutionEnvironment, а не в host process.

### Managed/multi-tenant run

Ядро работает как сервис с tenant isolation, quotas, remote MCP, registry skills, централизованной policy и observability.

## Основной сценарий

1. Клиент или peer agent отправляет A2A Message с prompt и Core extension MCP/skills.
2. Ядро валидирует входы и создаёт неизменяемый snapshot расширений.
3. Ядро создаёт A2A Task, планирует шаги и стримит status/artifact updates.
4. Безопасные действия проходят автоматически.
5. Для рискованного действия ядро приостанавливается и запрашивает approval.
6. При заполнении контекста ядро выполняет compaction и продолжает задачу.
7. Независимая долгая работа уходит в background Task; agent продолжает работу или пассивно ждёт notification.
8. При необходимости ядро создаёт сабагента с узкой инструкцией и явными tool/MCP/skill allowlists.
9. Main и child agents читают общую Markdown-memory; write атомарно обновляет BM25/vector/graph/NER revision.
10. Runtime создаёт checkpoints до и после внешних side effects.
11. Task завершается Artifact/Message либо типизированной ошибкой; session и memory обновляются атомарно.

## Метрики успеха продукта

Итоговый продукт считается успешным, если:

- интеграция использует стандартные A2A Message/Task/Artifact operations и Core extension;
- клиентский код не содержит собственного agent loop;
- длинная задача переживает хотя бы два compaction без потери активной цели;
- ни одно действие, требующее approval, не исполняется до подтверждения;
- каждый tool call и approval восстанавливается из аудита;
- типовой запуск без MCP и skills работает с одним `prompt`;
- stateful session не требует от клиента вручную пересылать историю;
- после рестарта безопасно продолжается ожидающий или вычислительный run;
- пользователь может увидеть, исправить и удалить сохранённую о себе память;
- новая модель, skill или MCP transport подключаются через стабильный adapter contract.
- background Task не блокирует agent loop и доставляет durable notification;
- команды main/child agents никогда не исполняются на control-plane host;
- OTel trace связывает A2A Task, model, memory, tools, sandbox и сабагентов.

## Границы продукта

- Core Agent не обучает foundation models и не является model provider.
- Core Agent не является визуальным workflow builder; детерминированные workflows могут быть tools или skills.
- Core Agent не обещает математически lossless-суммаризацию естественного языка; он сохраняет raw transcript и проверяемые pinned facts.
- Core Agent не заменяет host IAM, secret store, billing и data warehouse; он интегрируется с ними через adapters.
- Делегирование служит выполнению одной пользовательской задачи, а не общему распределённому вычислительному фреймворку.

## Продуктовые принципы

### Простой край, сложное ядро

Сложность model loop, памяти, безопасности и восстановления скрыта за стабильным входом. «Просто» не означает неявно или небезопасно: важные решения видны в событиях.

### Safe autonomy

Ядро самостоятельно делает обратимые, локальные и разрешённые действия. Чем выше необратимость, внешний эффект или чувствительность данных, тем сильнее isolation и участие человека.

### Progressive disclosure везде

История, tool schemas, skills и memory загружаются по необходимости. Большой каталог возможностей не должен автоматически съедать контекст.

### Durable before clever

Нельзя ускорять agent loop ценой повторных side effects, потери approval или неаудируемого состояния.

### Provider-neutral semantics

Публичные гарантии не должны зависеть от названия конкретной модели. Provider-specific возможности доступны через capability negotiation и деградируют явно.
