import type { AgentSettings, HistoryItem, Interaction, Peer, Schedule, Settings, Task, ToolPolicy } from "../../ui/src/types";
import type { PeerConversation } from "../../ui/src/PeerConversations";

export type DemoMode = "normal" | "empty" | "error" | "loading";
export const markdown = `## Результат проверки
Подготовлены **три пункта** и файл с итогами.

- Проверены исходные данные.
- Выполнен расчёт.
- Результат сохранён.

| Показатель | Значение |
| --- | --- |
| Заказов | 12 |
| Сумма | 57 500 ₽ |

\`\`\`python
from decimal import Decimal
amount = Decimal("57500")
print(f"Итого: {amount:,.2f}")
\`\`\`

\`\`\`mermaid
flowchart LR
  A[Запрос] --> B{Нужно разрешение?}
  B -->|Да| C[Владелец]
  B -->|Нет| D[Выполнение]
  C --> D
  D --> E[Ответ]
\`\`\`
`;
export const demoFile = { file_id: "report", name: "report.md", media_type: "text/markdown", size_bytes: new TextEncoder().encode(markdown).length, sha256: "a".repeat(64) };
export const demoQuestion = "Для кого предназначена презентация? Укажите аудиторию, желаемое количество слайдов и основные темы.";
export function demoInteraction(kind: Interaction["kind"] = "tool_approval"): Interaction {
  return { wait_id: kind, source_id: "ask", context_id: "demo", kind, subject_digest: "demo-digest", deadline: Date.now() / 1000 + 86400, outcome: null,
    subject: kind === "tool_approval" ? { tool_name: "core_ask_owner", arguments: { question: demoQuestion } }
      : kind === "owner_question" ? { question: demoQuestion } : { affected_scope: "tool_result", source_kind: "tool_result", reason: "suspicious", source_id: "result:read" } };
}
export const demoPeer: PeerConversation = {
  operation_id: "peer-operation", peer_id: "analyst", peer_name: "Аналитик", root_task_id: "demo-task",
  state: "completed", remote_state: "TASK_STATE_COMPLETED", created_at: 1792051200, updated_at: 1792051260,
  revision: 1, message_count: 2, files_available: true, last_checked_at: 1792051260, next_check_at: null,
  last_message: "Расчёт завершён. Отчёт приложен.", error_code: null, outcome_unknown: false,
  request_delivery: "confirmed", material_status: "available",
};
export const demoHistory: HistoryItem[] = [
  { id: "answer", task_id: "demo-task", kind: "result", text: markdown, status: "available", outcome: { state: "COMPLETED" }, response_files: [demoFile] },
  { id: "read-result", task_id: "demo-task", kind: "tool_result", text: JSON.stringify({ tool_call_id: "read", tool_name: "core_terminal_exec", status: "succeeded", output: { stdout: "report.md\nnotes.txt\n", exit_code: 0 } }), status: "available" },
  { id: "read-call", task_id: "demo-task", kind: "tool_call", text: JSON.stringify([{ id: "read", name: "core_terminal_exec", arguments: { argv: ["ls", "-la"] } }]), status: "available" },
  { id: "user", task_id: "demo-task", kind: "user_message", text: "Проверь данные и подготовь краткий отчёт.", status: "available" },
];
export const demoTask: Task = { id: "demo-task", contextId: "demo", status: { state: "TASK_STATE_COMPLETED" } };
export const demoRow = { context_id: "demo", latest_task_id: "demo-task", active: false, title: "Проверка отчёта" };
const settings: Settings = { revision: 1, hitl_timeout_seconds: 86400, owner_answer_timeout_seconds: 86400, guardrails_timeout_seconds: 86400, attachment_limit_bytes: 25000000, remote_timeout_seconds: 86400, remote_poll_interval_seconds: 300 };
const profile: AgentSettings = { revision: 1, profile_prompt: "Помогай владельцам с документами и расчётами. Задавай вопросы, когда данных недостаточно.", model_id: "demo-model", mcp_servers: [{ name: "documents", url: "https://mcp.example.invalid/mcp", enabled: true, header_name: "Authorization", has_header_value: true }], inherits: { profile_prompt: false, model_id: false, mcp_servers: false } };
const tools: ToolPolicy[] = ["core_terminal_exec", "core_python_exec", "core_ask_owner", "core_agent_send_message", "core_cron_create"].map((canonical_name, i) => ({ canonical_name, origin: `builtin:${canonical_name}`, mode: i === 2 ? "allow" : i === 4 ? "deny" : "require_hitl", guardrails_exempt: i === 2, revision: 1 }));
const peer: Peer = { id: "analyst", name: "analyst", description: "Помогает с анализом данных и подготовкой отчётов", url: "https://agent.example.invalid/a2a/external/", enabled: true, header_name: "Authorization", has_header_value: true, revision: 1 };
const schedule: Schedule = { id: "daily", context_id: "demo", prompt: "Подготовь ежедневный отчёт", expression: "0 9 * * 1-5", timezone: "Europe/Moscow", enabled: true, revision: 1, next_due_at: "2027-01-15T06:00:00Z", active_task_id: null };
const json = (value: unknown, status = 200) => Response.json(value, { status });
const list = (field: string, values: unknown[]) => json({ [field]: values, next_cursor: null });

// No fetch, credentials, storage or fallback transport. Each story gets its own state.
export function createDemoRequest(mode: DemoMode = "normal", taskState = "TASK_STATE_COMPLETED") {
  let runtime = structuredClone(settings), configuration = structuredClone(profile);
  let policies = structuredClone(tools), peers = mode === "empty" ? [] : [structuredClone(peer)];
  let schedules = mode === "empty" ? [] : [structuredClone(schedule)];
  let history = mode === "empty" ? [] : structuredClone(demoHistory);
  let chats = mode === "empty" ? [] : [structuredClone(demoRow)];
  const task = structuredClone(demoTask); task.status.state = taskState;
  if (taskState !== "TASK_STATE_COMPLETED") history = history.filter(row => row.kind !== "result");
  let files = mode === "empty" ? [] : [{ name: "report.md", path: "report.md", size: demoFile.size_bytes, mtime_ns: 1792051200000000000, identity_token: "demo-file" }];
  let accounts = mode === "empty" ? [] : [{ id: "partner", name: "Партнёр", status: "active", created_at: Date.now() / 1000, issued_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 864000 }];
  return async (path: string, options: RequestInit = {}): Promise<Response> => {
    options.signal?.throwIfAborted();
    if (mode === "loading") return new Promise((_, reject) => options.signal?.addEventListener("abort", () => reject(options.signal?.reason), { once: true }));
    if (mode === "error") return json({ error: { code: "DEMO_UNAVAILABLE" } }, 503);
    if (!path.startsWith("/api/") && !path.startsWith("/a2a/owner/")) return json({ error: { code: "DEMO_ROUTE_NOT_FOUND" } }, 404);
    const url = new URL(path, "http://catalog.invalid"), route = decodeURIComponent(url.pathname);
    const method = options.method ?? "GET", body = typeof options.body === "string" ? JSON.parse(options.body) : {};
    if (route === "/api/settings") { if (method === "PUT") runtime = { ...runtime, ...body, revision: runtime.revision + 1 }; return json(runtime); }
    if (route === "/api/agent-settings/models") return json({ models: ["demo-model", "demo-reasoning", "demo-fast"] });
    if (route === "/api/agent-settings") { if (method === "PUT") configuration = { ...configuration, ...body, revision: configuration.revision + 1 }; return json(configuration); }
    if (route === "/api/tool-policies") return list("tools", policies);
    if (route.startsWith("/api/tool-policies/")) { policies = policies.map(p => p.canonical_name === route.split("/").at(-1) ? { ...p, ...body, revision: p.revision + 1 } : p); return json({}); }
    if (route === "/api/remote-agents") {
      if (method === "POST") peers.push({ ...peer, ...body, id: crypto.randomUUID(), has_header_value: !!body.header_value });
      return list("agents", peers);
    }
    if (route.startsWith("/api/remote-agents/")) {
      const id = route.split("/")[3];
      peers = method === "DELETE" ? peers.filter(p => p.id !== id) : peers.map(p => p.id === id ? { ...p, ...body, revision: p.revision + 1 } : p);
      return json({});
    }
    if (route.startsWith("/api/external-access")) {
      const id = route.split("/")[3];
      if (method === "POST") {
        const account = { id: id ?? crypto.randomUUID(), name: body.name ?? accounts.find(a => a.id === id)?.name ?? "Демо", status: "active", created_at: Date.now() / 1000, issued_at: Date.now() / 1000, expires_at: Date.now() / 1000 + body.days * 86400 };
        accounts = [...accounts.filter(a => a.id !== account.id), account];
        return json({ account, access_token: "DEMO-NOT-A-REAL-TOKEN", expires_in: body.days * 86400 });
      }
      if (method === "DELETE") accounts = route.endsWith("/account") ? accounts.filter(a => a.id !== id) : accounts.map(a => a.id === id ? { ...a, status: "revoked" } : a);
      return json({ accounts, total: accounts.length });
    }
    if (route === "/api/schedules") {
      if (method === "POST") { const entry = { ...schedule, ...body, id: crypto.randomUUID() }; schedules.push(entry); return json({ schedule: entry }); }
      return list("schedules", schedules);
    }
    if (route.startsWith("/api/schedules/")) {
      const id = route.split("/")[3];
      if (method === "DELETE") schedules = schedules.filter(s => s.id !== id);
      if (method === "PUT") schedules = schedules.map(s => s.id === id ? { ...s, ...body, revision: s.revision + 1 } : s);
      return json(route.endsWith("/run-now") ? { task } : { schedule: schedules.find(s => s.id === id) });
    }
    if (route === "/api/chats") return list("chats", chats);
    if (route.endsWith("/history")) return list("items", history);
    if (route === "/api/interactions") return list("interactions", []);
    if (/^\/api\/(hitl|questions|guardrails)\//.test(route)) return json(route.endsWith("/material")
      ? { review: { review_id: "PRIVATE-REVIEW-ID", revision: 4, created_at: 1792136460, source_kind: "tool_result", classification: { reason: "suspicious", completion_tokens: 648 } },
          material: { payload: { tool_name: "core_terminal_exec", tool_call_id: "PRIVATE-CALL-ID", status: "succeeded",
            output: { status: "succeeded", stdout: "# README\n\nИнструкции проекта.\n<script>window.materialExecuted=true</script>\nКонец материала.", stderr: "Предупреждение проверки\n", exit_code: 0, cleanup: "sandbox_terminated" } }, sealed_ref: null } }
      : { outcome: { reason: body.decision === "reject" ? "rejected" : "allowed", answer: body.answer } });
    if (route.includes("/peer-conversations")) {
      if (route.endsWith("/observation")) return json({});
      if (route.endsWith("/peer-conversations")) return list("conversations", mode === "empty" ? [] : [demoPeer]);
      return json({ ...demoPeer, messages: [{ id: "request", direction: "outgoing", text: "Подготовь краткий отчёт по заказам.", created_at: demoPeer.created_at }, { id: "reply", direction: "incoming", text: markdown, created_at: demoPeer.updated_at }], files: [], artifacts: [], outgoing_files: [], outgoing_files_status: "none", history_truncated: false, observation_expires_at: null });
    }
    if (route.endsWith("/files/delete")) {
      if (method === "GET") return json({ error: { code: "FILE_CLEANUP_NOT_FOUND" } }, 404);
      const chosen = body.files as { path: string; identity_token: string }[];
      files = files.filter(file => !chosen.some(item => item.path === file.path));
      return json({ request_id: body.request_id, operation_id: "demo-cleanup", files: chosen, state: "completed", workspace_revision: 2, results: chosen.map(file => ({ path: file.path, status: "deleted", size: demoFile.size_bytes })), totals: { deleted: chosen.length, skipped: 0, errors: 0, deleted_bytes: chosen.length * demoFile.size_bytes } });
    }
    if (route.endsWith("/files")) return json({ files, next_cursor: null, listed_at: Date.now() / 1000, active: false, workspace_revision: 1, cleanup_block_reason: null });
    if (route.endsWith("/files/data")) return json({ total: 57500, currency: "RUB", orders: 12 });
    if (route.includes("/files/")) return new Response(markdown, { headers: { "Content-Type": "text/markdown" } });
    if (route.endsWith("/title")) { chats = chats.map(row => row.context_id === "demo" ? { ...row, title: body.title } : row); return json({ ...chats[0], title_revision: 2 }); }
    if (route === "/api/chats/demo" && method === "DELETE") { chats = chats.filter(row => row.context_id !== "demo"); return json({ context_id: "demo", archived: true }); }
    if (route === "/a2a/owner/message:send") {
      task.status.state = "TASK_STATE_COMPLETED";
      if (!chats.length) chats.push(structuredClone(demoRow));
      history = [{ id: crypto.randomUUID(), task_id: task.id, kind: "result", status: "available", text: "Демонстрационный ответ. Реальный агент не вызывался.", outcome: { state: "COMPLETED" } }, { id: crypto.randomUUID(), task_id: task.id, kind: "user_message", status: "available", text: body.message.parts.map((p: { text?: string }) => p.text ?? "[Вложение]").join("\n") }, ...history];
      return json({ task });
    }
    if (route.endsWith(":subscribe")) return new Response(new ReadableStream({ start(controller) { options.signal?.addEventListener("abort", () => controller.close(), { once: true }); } }), { headers: { "Content-Type": "text/event-stream" } });
    if (route.startsWith("/a2a/owner/tasks/")) { if (route.endsWith(":cancel")) task.status.state = "TASK_STATE_CANCELED"; return json(task); }
    return json({ error: { code: "DEMO_ROUTE_NOT_FOUND" } }, 404);
  };
}
