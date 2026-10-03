const labels: Record<string, string> = {
  core_terminal_exec: "Выполнение команды",
  core_python_exec: "Выполнение Python",
  core_task_start: "Запуск фоновой задачи",
  core_task_get: "Проверка задачи",
  core_task_list: "Список задач",
  core_task_wait: "Ожидание задачи",
  core_task_cancel: "Остановка задачи",
  core_agent_send_message: "Сообщение внешнему агенту",
  core_delegate: "Передача задачи помощнику",
  core_ask_owner: "Вопрос владельцу",
  core_wait_until: "Ожидание времени",
  core_cron_create: "Создание расписания",
  core_response_begin: "Подготовка ответа",
  core_response_files: "Выбор файлов для ответа",
  core_memory_search: "Поиск в памяти",
  core_memory_read: "Чтение записи памяти",
  core_memory_create: "Создание записи памяти",
  core_memory_update: "Изменение записи памяти",
  core_memory_split: "Разделение записи памяти",
  core_memory_delete: "Удаление записи памяти",
  core_skill_activate: "Подключение навыка",
  core_skill_read_resource: "Чтение материала навыка",
};

function parameters(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function toolLabel(name: string): string {
  return labels[name] ?? name.replace(/^mcp[_:]+/, "").replace(/[_:]+/g, " ");
}

export function actionLabel(name: string, args: unknown): string {
  const values = parameters(args);
  if (name === "core_terminal_exec" && Array.isArray(values.argv)
    && typeof values.argv[0] === "string") return `Команда: ${values.argv[0]}`;
  if (name === "core_agent_send_message" && typeof values.agent_name === "string")
    return `Сообщение агенту «${values.agent_name}»`;
  if (name === "core_task_start" && typeof values.tool === "string")
    return `Фоновая задача: ${toolLabel(values.tool)}`;
  if (name === "core_memory_create" && typeof values.title === "string")
    return `Новая запись: ${values.title}`;
  if (name === "core_response_files" && Array.isArray(values.paths) && !values.paths.length)
    return "Очистка выбранных файлов ответа";
  return toolLabel(name);
}

// These labels describe saved parameters, never an inferred effect of a command.
const fields: Record<string, string> = {
  argv: "Команда и аргументы",
  command: "Команда",
  code: "Код Python",
  cwd: "Каталог выполнения",
  agent_name: "Адресат",
  recipient: "Адресат",
  recipients: "Адресаты",
  to: "Кому",
  subject: "Тема",
  task: "Задача",
  message: "Сообщение",
  text: "Текст",
  content: "Содержимое",
  body: "Содержимое",
  files: "Файлы",
  paths: "Файлы",
  attachments: "Вложения",
  path: "Путь",
  file_path: "Файл",
  file_id: "Файл",
  document_id: "Документ",
  object_id: "Объект изменения",
  memory_id: "Запись памяти",
  title: "Название",
  instruction: "Задача помощника",
  question: "Вопрос",
  prompt: "Запрос",
  expression: "Расписание",
  timezone: "Часовой пояс",
  schedule_id: "Расписание",
  task_id: "Задача",
  until: "Время ожидания",
  query: "Поисковый запрос",
  reason: "Причина",
  overview: "Обзор записи",
  children: "Новые записи",
};

export function actionPreview(name: string, args: unknown): Array<{ label: string; value: string }> {
  const values = parameters(args);
  const preview = Object.entries(fields).flatMap(([key, label]) => {
    const value = values[key];
    if (value === undefined || value === null || value === "") return [];
    let text: string;
    if (typeof value === "string") text = value;
    else if (Array.isArray(value) && value.every((entry) => typeof entry === "string"))
      text = key === "argv" ? value.map((entry) => JSON.stringify(entry)).join(" ")
        : value.length ? value.join("\n") : "Нет";
    else text = JSON.stringify(value, null, 2);
    return [{ label: name === "core_cron_create" && key === "prompt" ? "Запрос по расписанию"
      : name === "core_agent_send_message" && key === "task" ? "Сообщение" : label, value: text }];
  });
  if (name === "core_task_start" && typeof values.tool === "string" && values.tool !== name)
    preview.push(...actionPreview(values.tool, values.arguments));
  return preview;
}
