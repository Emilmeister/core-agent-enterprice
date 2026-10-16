export interface Identity {
  actor_id: string;
  role: "owner";
  tenant: string;
}
export interface ChatRow {
  context_id: string;
  latest_task_id: string | null;
  active: boolean;
  title?: string;
  title_revision?: number;
  updated_at?: number;
  status?: string | null;
  needs_attention?: boolean;
}
export interface AttachmentEntry {
  index: number;
  actual_name: string;
  relative_path: string;
  size_bytes: number;
  sha256: string;
}
export interface ResponseFileEntry {
  file_id: string;
  name: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
}
export interface ResponseArtifact {
  artifact_id: string;
  name: string;
  parts: (ResponseFileEntry & { type: "text" | "data" | "file" })[];
}

// Copy only public receipt fields; a malformed batch never yields a partial card.
export function responseArtifacts(value: unknown): ResponseArtifact[] {
  if (!Array.isArray(value) || value.length > 64) return [];
  const result: ResponseArtifact[] = [];
  let parts = 0;
  for (const artifact of value) {
    if (!artifact || typeof artifact !== "object" || typeof artifact.artifact_id !== "string"
      || !artifact.artifact_id || typeof artifact.name !== "string" || artifact.name.length > 256
      || !Array.isArray(artifact.parts) || !artifact.parts.length) return [];
    const entries: ResponseArtifact["parts"] = [];
    for (const part of artifact.parts) {
      if (++parts > 256 || !part || typeof part !== "object" || !["text", "data", "file"].includes(part.type)
        || typeof part.file_id !== "string" || !part.file_id || typeof part.name !== "string"
        || typeof part.media_type !== "string" || !part.media_type || part.media_type.length > 256
        || !Number.isSafeInteger(part.size_bytes) || part.size_bytes < 0
        || typeof part.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(part.sha256)) return [];
      entries.push({ type: part.type, file_id: part.file_id, name: part.name, media_type: part.media_type,
        size_bytes: part.size_bytes, sha256: part.sha256 });
    }
    result.push({ artifact_id: artifact.artifact_id, name: artifact.name, parts: entries });
  }
  return result;
}
export interface PeerArtifact {
  index: number;
  artifact_id: string;
  name: string;
  parts: {
    index: number;
    type: "text" | "data" | "file";
    name: string;
    media_type: string;
    relative_path: string;
    size_bytes: number;
    sha256: string;
  }[];
}
export interface FileReceipt {
  schema_version: 1;
  batch_id: string;
  created_at: number;
  source: string;
  total_bytes: number;
  entries: AttachmentEntry[];
}
export const byteCount = (bytes: number) => `${new Intl.NumberFormat("ru-RU").format(bytes)} байт`;
export function formatFileSize(bytes: number): string {
  const units = ["Б", "КБ", "МБ", "ГБ"];
  const unit = Math.min(units.length - 1, Math.floor(Math.log10(Math.max(1, bytes)) / 3));
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(bytes / 1000 ** unit)} ${units[unit]}`;
}
export function fileReceipt(value: unknown): FileReceipt | undefined {
  if (!value || typeof value !== "object") return undefined;
  const receipt = value as FileReceipt;
  if (receipt.schema_version !== 1 || typeof receipt.batch_id !== "string" || !receipt.batch_id
    || !Number.isFinite(receipt.created_at) || typeof receipt.source !== "string"
    || !Number.isSafeInteger(receipt.total_bytes) || receipt.total_bytes < 0
    || !Array.isArray(receipt.entries) || !receipt.entries.length
    || receipt.entries.some((entry) => !entry || typeof entry !== "object"
      || !Number.isSafeInteger(entry.index) || entry.index < 0
      || typeof entry.actual_name !== "string" || typeof entry.relative_path !== "string"
      || !Number.isSafeInteger(entry.size_bytes) || entry.size_bytes < 0
      || typeof entry.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(entry.sha256))) return undefined;
  return receipt;
}
export interface HistoryItem {
  id: string;
  input_message_id?: string;
  local_delivery?: "sending" | "uncertain" | "failed" | "accepted";
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
  display_text?: string;
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
  attachments?: AttachmentEntry[];
  response_files?: ResponseFileEntry[];
  response_artifacts?: ResponseArtifact[];
  public_message_id?: string;
  public_message?: { message_id: string; text: string };
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

export interface LocalMessage {
  id: string;
  text: string;
  taskId?: string;
  delivery: NonNullable<HistoryItem["local_delivery"]>;
}

// Local echoes never restore rejected material or infer identity from message text.
export function withLocalMessages(items: HistoryItem[], messages: LocalMessage[]): HistoryItem[] {
  const projected = items.map(item => {
    const local = messages.find(message => message.id === item.input_message_id
      && (!message.taskId || message.taskId === item.task_id));
    return local && ["queued", "pending_guardrail"].includes(item.status)
      ? { ...item, kind: "user_message" as const, status: "available" as const,
        text: local.text, display_text: local.text, local_delivery: local.delivery } : item;
  });
  const missing: HistoryItem[] = messages.filter(message => !items.some(item => item.input_message_id === message.id
    && (!message.taskId || message.taskId === item.task_id))).map(message => ({
      id: `local:${message.id}`, input_message_id: message.id, task_id: message.taskId ?? null,
      kind: "user_message", status: "available", text: message.text, local_delivery: message.delivery,
    }));
  return [...missing.reverse(), ...projected];
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
  data?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  raw?: string;
  url?: string;
  filename?: string;
  mediaType?: string;
}
export interface Message {
  messageId: string;
  role: string;
  parts: Part[];
  taskId?: string;
  contextId?: string;
  metadata?: Record<string, unknown>;
}
export interface Task {
  id: string;
  contextId: string;
  status: { state: string; message?: Message };
  history?: Message[];
  artifacts?: { artifactId: string; name?: string; parts: Part[]; metadata?: Record<string, unknown> }[];
  metadata?: Record<string, unknown>;
}
export interface LiveAnswerSnapshot {
  taskId: string;
  generation: number;
  sequence: number;
  text: string;
  superseded: boolean;
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
  source_id?: string;
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
export interface McpConnection {
  name: string;
  url: string;
  enabled: boolean;
  header_name: string;
  has_header_value: boolean;
}
export interface AgentSettings {
  revision: number;
  profile_prompt: string;
  model_id: string;
  mcp_servers: McpConnection[];
  inherits: { profile_prompt: boolean; model_id: boolean; mcp_servers: boolean };
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

// A canonical Task never grants visibility ahead of the current owner-history decision.
export function publicMessageText(item: HistoryItem, task?: Task): string {
  if (item.status !== "available") return "";
  const message = task?.status.message;
  if (task?.id === item.task_id && item.public_message_id && message?.messageId === item.public_message_id
    && ["ROLE_AGENT", "agent"].includes(message.role) && message.metadata?.partial !== true
    && Array.isArray(message.parts)) return textParts(message.parts);
  return item.text;
}

// Keep server pagination untouched and insert approved payloads beside their action.
export function publicHistoryItems(items: HistoryItem[]): HistoryItem[] {
  const rows: HistoryItem[] = [];
  const artifacts = new Set<string>();
  const messages = new Set<string>();
  for (const item of [...items].reverse()) {
    const { public_message, response_artifacts, ...ordinary } = item;
    const receipts = item.status === "available" ? responseArtifacts(response_artifacts).filter(artifact => {
      const key = JSON.stringify([item.task_id, artifact.artifact_id]);
      if (artifacts.has(key)) return false;
      artifacts.add(key);
      return true;
    }) : [];
    rows.push(ordinary);
    if (item.status === "available" && public_message && typeof public_message.message_id === "string"
      && typeof public_message.text === "string" && !messages.has(public_message.message_id)) {
      messages.add(public_message.message_id);
      rows.push({ id: item.id + "/message", task_id: item.task_id, kind: "agent_message", status: "available",
        text: public_message.text, public_message_id: public_message.message_id });
    }
    if (receipts.length) rows.push({ id: item.id + "/artifacts", task_id: item.task_id,
      kind: "result", status: "available", text: "", response_artifacts: receipts });
  }
  return rows.reverse();
}
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
