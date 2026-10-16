import { toolLabel } from "./toolPresentation";

export type MaterialSource = { name: string; args?: unknown };
function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
export function guardrailReason(reason: unknown): string {
  const reasons: Record<string, string> = {
    suspicious: "Обнаружены признаки инструкций, способных изменить поведение агента.",
    uncertain: "Автоматическая проверка не смогла определить, безопасен ли материал.",
    incomplete_extraction: "Не удалось полностью извлечь содержимое для проверки.",
    timeout: "Автоматическая проверка не завершилась вовремя.",
    budget_exhausted: "Достигнут лимит автоматической проверки материала.",
  };
  return typeof reason === "string" && Object.hasOwn(reasons, reason)
    ? reasons[reason] : "Не удалось автоматически проверить материал.";
}

export function GuardrailMaterial({ value, source }: { value: Record<string, unknown>; source?: MaterialSource }) {
  const material = record(value.material);
  const review = record(value.review);
  const payload = material.payload;
  const envelope = record(payload);
  const tool = source?.name ?? (typeof envelope.tool_name === "string" ? envelope.tool_name : "");
  const isResult = review.source_kind === "tool_result" || review.source_kind === "owner_answer";
  const content = isResult && Object.hasOwn(envelope, "output") ? envelope.output : payload;
  const output = record(content);
  const process = isResult && ["core_terminal_exec", "core_python_exec"].includes(tool)
    && (typeof output.stdout === "string" || typeof output.stderr === "string");
  const args = record(source?.args ?? (review.source_kind === "tool_arguments" ? payload : undefined));
  const command = tool === "core_terminal_exec" && Array.isArray(args.argv)
    && args.argv.every(arg => typeof arg === "string")
    ? args.argv.map((arg: string) => /\s/.test(arg) ? JSON.stringify(arg) : arg).join(" ") : "";
  const kinds: Record<string, string> = { initial_input: "Сообщение", follow_up: "Уточнение",
    file_attachment: "Прикреплённый файл", tool_arguments: "Аргументы инструмента", tool_result: "Результат инструмента", owner_answer: "Ответ владельца" };
  const origin = tool === "core_terminal_exec" ? "Терминал" : tool ? toolLabel(tool)
    : typeof review.source_kind === "string" && Object.hasOwn(kinds, review.source_kind) ? kinds[review.source_kind] : "Материал";
  const file = record(record(material.file).entry);
  const fileName = typeof file.original_name === "string" ? file.original_name : file.actual_name;
  return <>
    <p className="material-source"><span className="muted">Источник: </span>{origin}</p>
    {command && <pre className="material-command" aria-label="Команда">{command}</pre>}
    <div className="material-content" role="region" tabIndex={0} aria-label="Содержимое проверяемого материала">
      {material.sealed_ref ? <>
        {typeof fileName === "string" && <p>{fileName}</p>}
        <p>Скачайте проверяемый файл, чтобы ознакомиться с его содержимым.</p>
      </> : process ? <>
        {typeof output.stdout === "string" && output.stdout !== "" && <pre aria-label="Вывод команды">{output.stdout}</pre>}
        {typeof output.stderr === "string" && output.stderr !== "" && <><strong>Сообщения об ошибках и предупреждения</strong><pre>{output.stderr}</pre></>}
        {!output.stdout && !output.stderr && <p>Команда не вернула текстового вывода.</p>}
        {output.truncated === true && <p className="muted">Вывод был сокращён при выполнении команды.</p>}
      </> : <pre>{typeof content === "string" ? content : content === undefined ? "Материал недоступен." : JSON.stringify(content, null, 2)}</pre>}
    </div>
  </>;
}
