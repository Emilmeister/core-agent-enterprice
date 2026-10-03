import { actionLabel, actionPreview } from "./toolPresentation";
import type { HistoryItem, Interaction } from "./types";
import { CodeBlock } from "./Code";

type ToolResult = { status?: string; output?: unknown; error_code?: string };
type ActionState = { kind: "success" | "error" | "waiting" | "active" | "unknown"; label: string; reason?: string };
const verify = "Обновите историю и проверьте результат перед повтором действия.";
export interface HistoryAction {
  key: string;
  taskId: string | null;
  callId?: string;
  name: string;
  args?: unknown;
  item: HistoryItem;
  result?: ToolResult;
  raw?: string;
}
export type HistoryEntry = { item: HistoryItem; action?: HistoryAction };

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}
function parse(text: string): unknown {
  try { return JSON.parse(text); } catch { return undefined; }
}

// Canonical owner history has arrays of {id,name,arguments} and individual result objects.
// Join by Task as well as call ID; providers can reuse a call ID in another Task.
export function historyEntries(items: HistoryItem[]): HistoryEntry[] {
  const entries: HistoryEntry[] = [];
  const actions = new Map<string, HistoryAction>();
  function get(item: HistoryItem, callId: string) {
    const key = JSON.stringify([item.task_id, callId]);
    let action = actions.get(key);
    if (!action) {
      action = { key, taskId: item.task_id, callId, name: "", item };
      actions.set(key, action);
      entries.push({ item, action });
    }
    return action;
  }
  for (const item of [...items].reverse()) {
    if ((item.kind !== "tool_call" && item.kind !== "tool_result") || item.status !== "available") {
      entries.push({ item });
      continue;
    }
    const value = parse(item.text);
    if (item.kind === "tool_call" && Array.isArray(value) && value.length
      && value.every((entry) => typeof object(entry)?.id === "string" && typeof object(entry)?.name === "string")) {
      for (const call of value as { id: string; name: string; arguments?: unknown }[]) {
        const action = get(item, call.id);
        action.name = call.name;
        action.args = call.arguments;
      }
    } else if (item.kind === "tool_result" && typeof object(value)?.tool_call_id === "string") {
      const result = object(value)!;
      const action = get(item, result.tool_call_id as string);
      if (!action.name && typeof result.tool_name === "string") action.name = result.tool_name;
      action.result = {
        ...(typeof result.status === "string" ? { status: result.status } : {}),
        ...(Object.hasOwn(result, "output") ? { output: result.output } : {}),
        ...(typeof result.error_code === "string" ? { error_code: result.error_code } : {}),
      };
    } else {
      entries.push({ item, action: { key: item.id, taskId: item.task_id, name: "", item, raw: item.text } });
    }
  }
  return entries;
}

export function pendingAction(items: HistoryItem[], taskId: string): { name: string; args: unknown } | undefined {
  if (items.some((item) => item.task_id === taskId && item.outcome)) return undefined;
  const unfinished = historyEntries(items).filter((entry) => entry.action?.taskId === taskId
    && entry.action.callId && !entry.action.result && !entry.action.raw);
  // A model can declare several calls together; execution handles the first unresolved call.
  const latest = unfinished.at(-1)?.item.id;
  const action = unfinished.find((entry) => entry.item.id === latest)?.action;
  return action ? { name: action.name, args: action.args } : undefined;
}

function taskSnapshotState(value: unknown): ActionState {
  const snapshot = object(value);
  if (typeof snapshot?.task_id !== "string" || typeof snapshot.state !== "string")
    return { kind: "unknown", label: "Исход не определён", reason: `Состояние дочерней задачи недоступно. ${verify}` };
  const error = typeof snapshot.error === "string" ? snapshot.error.slice(0, 240) : "";
  const result = object(snapshot.result);
  const prefix = error ? error + " · " : "";
  if (error === "SIDE_EFFECT_UNKNOWN" || error === "RECOVERY_REQUIRES_RECONCILIATION" || result?.remote_outcome === "unknown")
    return { kind: "unknown", label: "Исход не определён",
      reason: `${prefix}${result?.reason === "timeout" ? "Срок ожидания внешней задачи истёк." : "Выполнение дочерней задачи не подтверждено."} ${verify}` };
  if (snapshot.state === "failed")
    return { kind: "error", label: error === "REMOTE_OPERATION_TIMEOUT" ? "Срок ожидания задачи истёк" : "Ошибка дочерней задачи",
      reason: prefix + "Дочерняя задача завершилась с ошибкой." };
  if (snapshot.state === "canceled")
    return { kind: "error", label: "Дочерняя задача отменена", reason: prefix + "Отмена дочерней задачи подтверждена." };
  if (snapshot.state === "submitted") return { kind: "waiting", label: "Дочерняя задача принята", reason: "Выполнение ещё не завершено." };
  if (snapshot.state === "working") return { kind: "waiting", label: "Дочерняя задача выполняется", reason: "Итоговый результат ещё не получен." };
  if (snapshot.state === "completed") return { kind: "success", label: "Дочерняя задача завершена" };
  return { kind: "unknown", label: "Исход не определён", reason: `${prefix}Сохранённое состояние задачи не подтверждает завершение. ${verify}` };
}

export function actionState(action: HistoryAction, terminal?: HistoryItem, waits: Interaction[] = []): ActionState {
  const output = object(action.result?.output);
  const error = action.result?.error_code;
  // Only process built-ins return ExecutionResult; business/MCP payload fields are not lifecycle state.
  const process = ["core_terminal_exec", "core_python_exec"].includes(action.name) ? output : undefined;
  if (error === "SIDE_EFFECT_UNKNOWN" || action.result?.status === "unknown" || process?.status === "unknown")
    return { kind: "unknown", label: "Исход не определён", reason: `${error ? error + " · " : ""}Выполнение не подтверждено. ${verify}` };
  if (action.result) {
    const status = process?.status ?? action.result.status;
    const timedOut = status === "timed_out" || process?.timed_out === true;
    if (action.result.status === "failed" || ["failed", "timed_out", "cancelled", "denied", "rejected", "aborted"].includes(String(status))
      || process?.timed_out === true || (typeof process?.exit_code === "number" && process.exit_code !== 0))
      return { kind: "error", label: timedOut ? "Время выполнения истекло" : "Ошибка действия",
        reason: `${error ? error + " · " : ""}${timedOut ? "Действие не завершилось за отведённое время."
          : typeof process?.exit_code === "number" && process.exit_code !== 0 ? `Процесс завершился с кодом ${process.exit_code}.`
          : ["cancelled", "aborted"].includes(String(status)) ? "Действие было остановлено."
          : ["denied", "rejected"].includes(String(status)) ? "Выполнение действия отклонено." : "Инструмент сообщил об ошибке."}` };
    if (["waiting", "pending", "running"].includes(String(status))) return { kind: "waiting", label: "Ожидает завершения" };
    if (action.result.status === "succeeded") {
      if (action.name === "core_agent_send_message" && typeof output?.success === "boolean")
        return output.success ? { kind: "success", label: "Ответ внешнего агента получен" }
          : { kind: "error", label: "Ошибка внешнего вызова", reason: typeof output.message === "string"
            ? output.message.slice(0, 240) : "Внешний агент не вернул итоговый результат." };
      if (["core_task_start", "core_task_get", "core_task_wait", "core_task_cancel", "core_delegate", "core_agent_send_message"].includes(action.name))
        return taskSnapshotState(output);
      if (action.name === "core_task_list") {
        if (!Array.isArray(action.result.output)) return taskSnapshotState(undefined);
        const snapshots = action.result.output.map(taskSnapshotState);
        return snapshots.find((item) => item.kind === "unknown") ?? snapshots.find((item) => item.kind === "error")
          ?? snapshots.find((item) => item.kind === "waiting") ?? { kind: "success", label: "Список задач получен" };
      }
      return { kind: "success", label: "Выполнено" };
    }
    return { kind: "unknown", label: "Исход не определён", reason: `${error ? error + " · " : ""}Статус результата недоступен. ${verify}` };
  }
  const wait = waits.find((item) => !item.outcome
    && action.callId !== undefined && (item.source_id === action.callId || item.subject.tool_call_id === action.callId
      || object(item.subject.call)?.id === action.callId || item.subject.source_id === "arguments:" + action.callId
      || item.subject.source_id === "result:" + action.callId));
  if (wait) return { kind: "waiting", label: wait.kind === "tool_approval" ? "Нужно ваше разрешение"
    : wait.kind === "owner_question" ? "Ожидает вашего ответа" : "Ожидает проверки материала" };
  if (terminal?.outcome || action.raw) return { kind: "unknown", label: "Исход не определён",
    reason: `${terminal?.outcome?.error_code ? terminal.outcome.error_code + " · " : ""}Результат этого действия недоступен. ${verify}` };
  return { kind: "active", label: "Ожидает результата" };
}

export function ActionCard({ action, terminal, waits }: {
  action: HistoryAction; terminal?: HistoryItem; waits?: Interaction[];
}) {
  const state = actionState(action, terminal, waits);
  const output = object(action.result?.output);
  const stdout = typeof output?.stdout === "string" ? output.stdout : undefined;
  const stderr = typeof output?.stderr === "string" ? output.stderr : undefined;
  const preview = action.name ? actionPreview(action.name, action.args) : [];
  const pythonSource = action.name === "core_python_exec" && typeof object(action.args)?.code === "string"
    ? object(action.args)!.code as string : undefined;
  const compact = (text: string) => text.length > 240 ? text.slice(0, 239) + "…" : text;
  const hasOutput = stdout !== undefined || stderr !== undefined || action.result?.output !== undefined;
  return <article className={`action-card action-${state.kind}`} data-action-key={action.key}
    id={`history-${action.item.id}-${action.callId ?? "unstructured"}`} data-history-id={action.item.id}>
    <div className="action-heading"><strong>{action.name ? compact(actionLabel(action.name, action.args)) : "Действие агента"}</strong>
      <span className={`action-status ${state.kind === "error" || state.kind === "unknown" ? "error" : "muted"}`}>{state.label}</span></div>
    {preview.length > 0 && <dl className="action-preview">{preview.map((field, index) => <div key={`${field.label}:${index}`}>
      <dt>{field.label}</dt><dd className="prose">{field.label === "Код Python"
        ? <CodeBlock text={compact(field.value)} language="python" /> : compact(field.value)}</dd>
    </div>)}</dl>}
    {state.reason && <p className={state.kind === "error" ? "error" : "muted"}>{state.reason}</p>}
    {hasOutput && <details className="action-output"><summary>Вывод действия</summary>
      {stdout !== undefined || stderr !== undefined ? <>
        {stdout && <><div className="eyebrow">stdout</div><pre>{stdout}</pre></>}
        {stderr && <><div className="eyebrow">stderr</div><pre>{stderr}</pre></>}
        {!stdout && !stderr && <p className="muted">Действие завершилось без текстового вывода.</p>}
        {output?.truncated === true && <p className="muted">Вывод ограничен при выполнении действия.</p>}
      </> : <pre>{typeof action.result?.output === "string" ? action.result.output : JSON.stringify(action.result?.output, null, 2)}</pre>}
    </details>}
    <details className="action-technical"><summary>Технические данные</summary><dl>
      <dt>Инструмент</dt><dd>{action.name || "Неизвестен"}</dd>
      {pythonSource !== undefined && <><dt>Код Python</dt><dd><CodeBlock text={pythonSource} language="python" /></dd></>}
      <dt>Аргументы</dt><dd><pre>{action.args === undefined ? "Недоступны" : JSON.stringify(action.args, null, 2)}</pre></dd>
      <dt>ID вызова</dt><dd>{action.callId ?? "Недоступен"}</dd><dt>ID задачи</dt><dd>{action.taskId ?? "Недоступен"}</dd>
      {typeof output?.exit_code === "number" && <><dt>Exit code</dt><dd>{output.exit_code}</dd></>}
      {action.result?.status && <><dt>Статус инструмента</dt><dd>{action.result.status}</dd></>}
      {action.raw && <><dt>Сохранённая запись</dt><dd><pre>{action.raw}</pre></dd></>}
    </dl></details>
  </article>;
}
