export interface Identity {
  actor_id: string;
  role: "owner";
  tenant: string;
}
export interface ChatRow {
  context_id: string;
  latest_task_id: string | null;
  active: boolean;
}
export interface HistoryItem {
  id: string;
  task_id: string | null;
  kind:
    | "user_message"
    | "agent_message"
    | "tool_call"
    | "tool_result"
    | "result"
    | "placeholder"
    | "schedule_notice";
  text: string;
  status:
    | "available"
    | "queued"
    | "pending_guardrail"
    | "rejected"
    | "timed_out"
    | "unprocessed_due_to_failure"
    | "unprocessed_due_to_cancel";
  schedule_id?: string;
  schedule_revision?: number;
  timezone?: string;
  reason?: string;
  due_at?: string | null;
  through?: string | null;
  created_at?: string;
  review?: { wait_id: string };
  outcome?: {
    state: string;
    complete?: boolean;
    completion_reason?: string;
    error_code?: string;
  };
}
export interface HistoryPage {
  items: HistoryItem[];
  next_cursor: string | null;
}

// Items and cursors remain in the server's newest-first order. Rendering reverses a copy.
export function mergeHistoryPage(
  current: HistoryPage,
  page: HistoryPage,
  older = false,
): HistoryPage {
  if (older) {
    const items = new Map(current.items.map((item) => [item.id, item]));
    for (const item of page.items) items.set(item.id, item);
    return { items: [...items.values()], next_cursor: page.next_cursor };
  }
  const ids = new Set(page.items.map((item) => item.id));
  const overlap = current.items.reduce(
    (last, item, index) => (ids.has(item.id) ? index : last),
    -1,
  );
  if (overlap < 0 || page.next_cursor === null) return page;
  const tail = current.items.slice(overlap + 1);
  return {
    items: [...page.items, ...tail],
    next_cursor: tail.length ? current.next_cursor : page.next_cursor,
  };
}
export interface Part {
  text?: string;
  metadata?: Record<string, unknown>;
  raw?: string;
  url?: string;
}
export interface Message {
  messageId: string;
  role: string;
  parts: Part[];
  taskId?: string;
  contextId?: string;
}
export interface Task {
  id: string;
  contextId: string;
  status: { state: string; message?: Message };
  history?: Message[];
  artifacts?: { artifactId: string; parts: Part[] }[];
  metadata?: Record<string, unknown>;
}
export interface RemoteProgress {
  task_id: string;
  revision: number;
  agent_name: string;
  remote_state: string;
}
export const remoteStates: Record<string, string> = {
  TASK_STATE_SUBMITTED: "Задача принята внешним агентом",
  TASK_STATE_WORKING: "Внешний агент выполняет задачу",
  TASK_STATE_INPUT_REQUIRED: "Внешний агент ждёт решения своего владельца",
  TASK_STATE_AUTH_REQUIRED: "Внешний агент ждёт авторизации",
};
export function remoteProgress(task?: Task): RemoteProgress[] {
  const entries = task?.metadata?.core_agent_remote_progress;
  if (!Array.isArray(entries) || terminal(task)) return [];
  return entries.filter(
    (entry): entry is RemoteProgress =>
      entry !== null &&
      typeof entry === "object" &&
      typeof entry.task_id === "string" &&
      typeof entry.agent_name === "string" &&
      Number.isSafeInteger(entry.revision) &&
      entry.revision > 0 &&
      typeof entry.remote_state === "string" &&
      Object.hasOwn(remoteStates, entry.remote_state),
  );
}
export interface Interaction {
  wait_id: string;
  context_id: string;
  kind: "tool_approval" | "owner_question" | "guardrail";
  subject: Record<string, unknown>;
  subject_digest: string;
  deadline: number;
  outcome: Record<string, unknown> | null;
}
export interface Settings {
  revision: number;
  hitl_timeout_seconds: number;
  owner_answer_timeout_seconds: number;
  guardrails_timeout_seconds: number;
  attachment_limit_bytes: number;
  remote_timeout_seconds: number;
  remote_poll_interval_seconds: number;
}
export interface ToolPolicy {
  canonical_name: string;
  origin: string;
  mode: "allow" | "require_hitl" | "deny";
  guardrails_exempt: boolean;
  revision: number;
}
export interface Peer {
  id: string;
  name: string;
  url: string;
  description: string;
  enabled: boolean;
  header_name: string;
  has_header_value: boolean;
  revision: number;
}
export const terminal = (task?: Task) =>
  !!task &&
  [
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
  ].includes(task.status.state);
export const textParts = (parts: Part[]) =>
  parts
    .filter(
      (p) => typeof p.text === "string" && p.metadata?.adk_thought !== true,
    )
    .map((p) => p.text)
    .join("\n");
export const states: Record<string, string> = {
  TASK_STATE_SUBMITTED: "Принята",
  TASK_STATE_WORKING: "В работе",
  TASK_STATE_INPUT_REQUIRED: "Ожидает решения",
  TASK_STATE_AUTH_REQUIRED: "Ожидает авторизации",
  TASK_STATE_COMPLETED: "Завершена",
  TASK_STATE_FAILED: "Ошибка",
  TASK_STATE_CANCELED: "Отменена",
  TASK_STATE_REJECTED: "Отклонена",
};

export interface Schedule {
  id: string;
  revision: number;
  context_id: string;
  prompt: string;
  expression: string;
  timezone: string;
  enabled: boolean;
  next_due_at: string | null;
  active_task_id: string | null;
}
