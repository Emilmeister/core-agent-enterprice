import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Api, errorText } from "./api";
import { FileCard } from "./FileCard";
import { Markdown } from "./Markdown";
import { remoteStates } from "./types";
import type { AttachmentEntry, ResponseFileEntry } from "./types";
import type { HistoryAction } from "./ActionCard";

export interface PeerConversation {
  operation_id: string;
  peer_id: string;
  peer_name: string;
  root_task_id: string;
  state: string;
  remote_state: string | null;
  created_at: number;
  updated_at: number;
  revision: number;
  message_count: number;
  files_available: boolean;
  last_checked_at: number | null;
  next_check_at: number | null;
  last_message: string;
  error_code: string | null;
  outcome_unknown: boolean;
  request_delivery: "not_sent" | "unconfirmed" | "confirmed";
  material_status: "available" | "pending_guardrail" | "rejected" | "timed_out";
}
interface PeerConversationDetail extends PeerConversation {
  messages: { id: string; direction: "outgoing" | "incoming"; text: string; created_at: number }[];
  files: AttachmentEntry[];
  outgoing_files: ResponseFileEntry[];
  outgoing_files_status: "none" | "available" | "unavailable";
  history_truncated: boolean;
  observation_expires_at: number | null;
}
export const peerActive = (value: Pick<PeerConversation, "state">) => ["submitted", "working"].includes(value.state);
export function peerOperationId(action?: HistoryAction): string | undefined {
  if (action?.name !== "core_agent_send_message") return undefined;
  const output = action.result?.output;
  if (!output || typeof output !== "object") return undefined;
  const id = (output as { task_id?: unknown }).task_id;
  return typeof id === "string" ? id : undefined;
}
const stateLabels: Record<string, string> = {
  submitted: "Готовим отправку", working: "Ожидаем данные от агента", completed: "Ответ получен",
  failed: "Поручение завершилось с ошибкой", canceled: "Поручение отменено",
};
const materialLabels = {
  pending_guardrail: "Обработка поручения",
  rejected: "Материал отклонён",
  timed_out: "Проверка материала не завершилась вовремя",
};
function status(value: PeerConversation): string {
  if (value.outcome_unknown) return "Не удалось подтвердить отправку поручения";
  if (value.error_code === "REMOTE_OPERATION_TIMEOUT") return "Время ожидания ответа истекло";
  return peerActive(value) && value.remote_state
    ? remoteStates[value.remote_state] ?? stateLabels[value.state] ?? "Ожидаем ответ"
    : stateLabels[value.state] ?? "Состояние неизвестно";
}
function path(contextId: string, operationId?: string): string {
  const base = `/api/chats/${encodeURIComponent(contextId)}/peer-conversations`;
  return operationId ? `${base}/${encodeURIComponent(operationId)}` : base;
}

export function usePeerConversations(api: Api, contextId: string | undefined, active: boolean) {
  const [conversations, setConversations] = useState<PeerConversation[]>([]);
  const [error, setError] = useState("");
  const current = useRef(conversations);
  const lifetime = useRef(0);
  useEffect(() => {
    setConversations([]);
    current.current = [];
    setError("");
  }, [api, contextId]);
  useEffect(() => {
    if (!contextId) return;
    const generation = ++lifetime.current;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pending = false;
    async function load() {
      clearTimeout(timer);
      if (pending || abort.signal.aborted || document.hidden) return;
      pending = true;
      try {
        const rows = await api.pages<PeerConversation>(path(contextId!), "conversations", abort.signal);
        if (!abort.signal.aborted && generation === lifetime.current) {
          current.current = rows;
          setConversations(rows);
          setError("");
        }
      } catch (failure) {
        if (!abort.signal.aborted) setError(errorText(failure));
      } finally {
        pending = false;
        if (!abort.signal.aborted && !document.hidden && (active || current.current.some(peerActive)))
          timer = setTimeout(() => void load(), 15000);
      }
    }
    const visible = () => { if (document.hidden) clearTimeout(timer); else void load(); };
    document.addEventListener("visibilitychange", visible);
    void load();
    return () => { abort.abort(); clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [api, contextId, active]);
  return { conversations, error };
}

export function PeerConversationCard({ conversation, onOpen }: {
  conversation: PeerConversation;
  onOpen: (value: PeerConversation, trigger: HTMLButtonElement) => void;
}) {
  const failed = conversation.state === "failed" || conversation.state === "canceled";
  return <article className="peer-conversation-card" data-peer-operation={conversation.operation_id}>
    <div className="peer-card-heading">
      <strong>{conversation.peer_name}</strong>
      <span className={failed ? "peer-status peer-status-error" : "peer-status"} role="status">{status(conversation)}</span>
    </div>
    {conversation.last_message && <p className="peer-card-preview">{conversation.last_message}</p>}
    {conversation.material_status !== "available" && <p className="history-note" role="status">{materialLabels[conversation.material_status]}</p>}
    <div className="peer-card-footer">
      <span className="muted">{conversation.message_count} сообщений{conversation.files_available ? " · Есть файлы" : ""}</span>
      <button type="button" className="secondary" aria-controls="peer-conversation-panel"
        onClick={(event) => onOpen(conversation, event.currentTarget)}>Переписка</button>
    </div>
  </article>;
}

// Cache each operation against the public summary used to request it. A new
// material verdict must hide stale content before the next detail read finishes.
const summaryKey = (row: PeerConversation) => JSON.stringify([
  row.operation_id, row.revision, row.state, row.files_available, row.material_status,
]);
type CachedDetail = { summary: string; materialStatus: PeerConversation["material_status"]; value: PeerConversationDetail };

export function PeerConversationPanel({ api, contextId, selected, conversations, onSelect, onClose }: {
  api: Api; contextId: string; selected: string; conversations: PeerConversation[];
  onSelect: (id: string) => void; onClose: () => void;
}) {
  const [details, setDetails] = useState<Record<string, CachedDetail>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [newMessages, setNewMessages] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [historyLimit, setHistoryLimit] = useState(20);
  const scroll = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const bottom = useRef(true);
  const cache = useRef<Record<string, CachedDetail>>({});
  const olderAnchor = useRef<{ height: number; top: number } | null>(null);
  const current = useRef(conversations);
  current.current = conversations;
  const peers = [...new Map([...conversations].reverse().map((row) => [row.peer_id, row])).values()];
  const operations = conversations.filter((row) => row.peer_id === selected);
  const shown = operations.filter((row, index) => index < historyLimit || peerActive(row));
  const signature = JSON.stringify(shown.map(summaryKey));
  useEffect(() => { closeButton.current?.focus(); }, []);
  useEffect(() => {
    cache.current = {};
    olderAnchor.current = null;
    setDetails({});
    setHistoryLimit(20);
    bottom.current = true;
    setNewMessages(false);
  }, [api, contextId, selected]);
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pending = false;
    let first = true;
    setError("");
    setLoading(true);
    async function load() {
      clearTimeout(timer);
      if (pending || abort.signal.aborted || document.hidden) return;
      pending = true;
      let failed = false;
      const rows = current.current.filter((row) => row.peer_id === selected)
        .filter((row, index) => index < historyLimit || peerActive(row));
      try {
        // Limit concurrent owner reads; completed history is cached, while every
        // active operation of the selected peer retains its own observation TTL.
        const reads = rows.filter((row) => peerActive(row) || cache.current[row.operation_id]?.summary !== summaryKey(row)
          || first && refreshKey > 0);
        first = false;
        for (let start = 0; start < reads.length && !abort.signal.aborted; start += 4) {
          await Promise.all(reads.slice(start, start + 4).map(async (row) => {
            try {
              const value = await api.json<PeerConversationDetail>(path(contextId, row.operation_id), "GET", undefined, abort.signal);
              if (abort.signal.aborted) return;
              const old = cache.current[row.operation_id]?.value;
              const changed = JSON.stringify(old?.messages) !== JSON.stringify(value.messages)
                || JSON.stringify(old?.files) !== JSON.stringify(value.files)
                || JSON.stringify(old?.outgoing_files) !== JSON.stringify(value.outgoing_files);
              cache.current = { ...cache.current, [row.operation_id]: { summary: summaryKey(row), materialStatus: row.material_status, value } };
              setDetails(cache.current);
              if (changed && !bottom.current && !olderAnchor.current) setNewMessages(true);
              if (changed && bottom.current) requestAnimationFrame(() => {
                if (!abort.signal.aborted && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
              });
              if (peerActive(value) && !document.hidden)
                await api.json(path(contextId, row.operation_id) + "/observation", "POST", { visible: true }, abort.signal);
            } catch (failure) {
              if (!abort.signal.aborted) { failed = true; setError(errorText(failure)); }
            }
          }));
        }
        if (!failed && !abort.signal.aborted) setError("");
      } finally {
        pending = false;
        if (!abort.signal.aborted) {
          setLoading(false);
          if (!document.hidden && (failed || rows.some(peerActive))) timer = setTimeout(() => void load(), 15000);
        }
      }
    }
    const visible = () => { if (document.hidden) clearTimeout(timer); else void load(); };
    document.addEventListener("visibilitychange", visible);
    void load();
    return () => { abort.abort(); clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [api, contextId, selected, refreshKey, historyLimit, signature]);
  const available = shown.flatMap((row) => {
    const cached = details[row.operation_id];
    return cached?.materialStatus === row.material_status ? [cached.value] : [];
  });
  // Namespacing by operation prevents a peer's reused message IDs from colliding.
  const entries = available.flatMap((detail) => [
    ...detail.messages.map((message, order) => ({ order, key: detail.operation_id + ":" + message.id,
      at: message.created_at, detail, message })),
    { order: detail.messages.length, key: detail.operation_id + ":status", at: Math.max(detail.updated_at, ...detail.messages.map((message) => message.created_at)),
      detail, message: null },
  ]).sort((a, b) => a.at - b.at || a.detail.created_at - b.detail.created_at
    || a.detail.operation_id.localeCompare(b.detail.operation_id) || a.order - b.order);
  useLayoutEffect(() => {
    const anchor = olderAnchor.current;
    if (anchor && scroll.current) {
      scroll.current.scrollTop = anchor.top + scroll.current.scrollHeight - anchor.height;
      if (!loading) olderAnchor.current = null;
    }
  }, [details, loading]);
  const lastChecked = Math.max(0, ...available.map((row) => row.last_checked_at ?? 0));
  const selectedName = operations[0]?.peer_name ?? "Внешний агент";
  const sameNames = peers.filter((row) => row.peer_name === selectedName).length > 1;
  return <aside className="peer-conversation-panel" id="peer-conversation-panel" aria-label="Переписка агентов"
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
    <header className="peer-panel-heading">
      <div><h2>Переписка</h2><p className="muted">{selectedName}</p></div>
      <button ref={closeButton} type="button" className="text-button" aria-label="Закрыть переписку агентов" onClick={onClose}>×</button>
    </header>
    <label className="peer-agent-select">Агент
      <select value={selected} onChange={(event) => onSelect(event.target.value)}>
        {peers.map((row) => <option value={row.peer_id} key={row.peer_id}>
          {row.peer_name}{peers.filter((peer) => peer.peer_name === row.peer_name).length > 1
            ? " · подключение " + row.peer_id.slice(0, 8) : ""}
        </option>)}
      </select>
    </label>
    {sameNames && <p className="peer-panel-status">У подключений с одинаковым именем отдельная история.</p>}
    {!!operations.filter(peerActive).length && <p className="peer-panel-status" role="status" aria-live="polite">
      Активных поручений: {operations.filter(peerActive).length}
    </p>}
    <div className="peer-messages" ref={scroll} tabIndex={0} role="region" aria-label="Сообщения агентов"
      onScroll={() => {
        if (scroll.current) {
          bottom.current = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 80;
          if (bottom.current) setNewMessages(false);
        }
      }}>
      {operations.length > shown.length && <button className="text-button" disabled={loading} onClick={() => {
        if (scroll.current) olderAnchor.current = { height: scroll.current.scrollHeight, top: scroll.current.scrollTop };
        bottom.current = false;
        setHistoryLimit((limit) => limit + 20);
      }}>Загрузить предыдущие поручения</button>}
      {loading && <p className="muted" role="status">Загружаем переписку…</p>}
      {entries.map(({ key, detail, message }) => message ? <article className={`peer-message peer-${message.direction}`}
        key={key} data-peer-message={key} data-peer-operation={detail.operation_id}>
        <div className="peer-message-heading"><strong>{message.direction === "outgoing"
          ? detail.request_delivery === "confirmed" ? "Поручение" : detail.request_delivery === "unconfirmed" ? "Отправка не подтверждена" : "Готовим поручение"
          : detail.peer_name}</strong>
          <time dateTime={new Date(message.created_at * 1000).toISOString()}>{new Date(message.created_at * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</time>
        </div>
        <Markdown text={message.text} />
        {message.direction === "outgoing" && !!detail.outgoing_files.length && <ul className="message-attachments">
          {detail.outgoing_files.map((file) => <FileCard key={file.file_id} api={api} contextId={contextId} file={file}
            available={detail.outgoing_files_status === "available"}
            downloadPath={`${path(contextId, detail.operation_id)}/outgoing-files/${encodeURIComponent(file.file_id)}`} />)}
        </ul>}
      </article> : <section className="peer-operation-outcome" key={key} data-peer-operation={detail.operation_id}>
        <p className="peer-operation-status" role="status">{status(detail)}</p>
        {detail.material_status !== "available" && <p className="history-note" role="status">{materialLabels[detail.material_status]}</p>}
        {detail.history_truncated && <p className="history-note">Показана доступная часть переписки по поручению.</p>}
        {!!detail.files.length && <section className="peer-files" aria-label="Файлы от внешнего агента"><h3>Файлы</h3>
          <ul className="message-attachments">{detail.files.map((file) => <FileCard key={file.relative_path} api={api} contextId={contextId} file={file} />)}</ul>
        </section>}
      </section>)}
      {!loading && !available.some((row) => row.messages.length) && <p className="muted">Внешний агент ещё не опубликовал сообщения.</p>}
      {operations.some(peerActive) && <p className="peer-availability-note muted">Здесь появятся сообщения и файлы, которые внешний агент передаёт по A2A.</p>}
    </div>
    {newMessages && <button className="secondary peer-new-messages" onClick={() => {
      if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
      bottom.current = true;
      setNewMessages(false);
    }}>Новые сообщения ↓</button>}
    <footer className="peer-panel-footer">
      {error && <p className="error" role="alert">Не удалось обновить переписку. {error}</p>}
      <div className="section-line"><span className="muted">{lastChecked
        ? `Получено ${new Date(lastChecked * 1000).toLocaleTimeString("ru-RU")}` : "Данных от внешнего агента пока нет"}</span>
        <button type="button" className="text-button" disabled={loading} onClick={() => setRefreshKey((key) => key + 1)}>Обновить</button>
      </div>
    </footer>
  </aside>;
}
