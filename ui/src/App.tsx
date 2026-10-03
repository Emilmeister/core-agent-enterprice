import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Session } from "./auth";
import { Api, errorText } from "./api";
import type { ChatRow, Task } from "./types";
import { terminal } from "./types";
import { Chat } from "./Chat";
import { Settings, ToolPolicies } from "./Settings";
import { Agents } from "./Agents";
import { Schedules } from "./Schedules";
import { ExternalAccess } from "./ExternalAccess";

type Page = "chats" | "tools" | "agents" | "settings" | "schedules" | "access";
const pages: [Page, string][] = [
  ["schedules", "Расписания"],
  ["tools", "Инструменты"],
  ["agents", "Агенты"],
  ["access", "Доступ к агенту"],
  ["settings", "Настройки"],
];
export function App({ session }: { session: Session }) {
  const api = useMemo(() => new Api(session), [session]);
  const [page, setPage] = useState<Page>("chats");
  const [chats, setChats] = useState<ChatRow[]>([]);
  const [selected, setSelected] = useState<ChatRow | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [menu, setMenu] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const [newKey, setNewKey] = useState(0);
  const [dirty, setDirty] = useState(false);
  const chatRead = useRef(0);
  const selectedRow = selected
    ? (chats.find((row) => row.context_id === selected.context_id) ?? selected)
    : null;
  function navigate(action: () => void) {
    if (
      !dirty ||
      window.confirm(
        "Есть неотправленное сообщение или неподтверждённый запрос. При переходе черновик и возможность повтора с тем же идентификатором будут потеряны. Перейти?",
      )
    )
      action();
  }
  const refreshChats = useCallback(
    async (signal?: AbortSignal) => {
      const read = ++chatRead.current;
      const rows = await api.pages<ChatRow>("/api/chats", "chats", signal);
      if (!signal?.aborted && read === chatRead.current) {
        setChats(rows.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || a.context_id.localeCompare(b.context_id)));
        setError("");
        setLoading(false);
      }
    },
    [api],
  );
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        await refreshChats(abort.signal);
      } catch (failure) {
        if (!abort.signal.aborted) {
          setError(errorText(failure));
          setLoading(false);
        }
      }
      if (!abort.signal.aborted) timer = setTimeout(load, 15000);
    }
    void load();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [refreshChats]);
  function choose(row: ChatRow | null) {
    navigate(() => {
      setSelected(row);
      setPage("chats");
      setMenu(false);
      if (!row) setNewKey((key) => key + 1);
    });
  }
  function submitted(task: Task) {
    const row = {
      context_id: task.contextId,
      latest_task_id: task.id,
      active: !terminal(task),
      status: task.status.state,
    };
    setSelected(row);
    void refreshChats().catch((e) => setError(errorText(e)));
  }
  function renamed(metadata: Partial<ChatRow> & { context_id: string }) {
    setChats((rows) => rows.map((row) => row.context_id === metadata.context_id ? { ...row, ...metadata } : row));
    setSelected((row) => row?.context_id === metadata.context_id ? { ...row, ...metadata } : row);
    void refreshChats().catch((e) => setError(errorText(e)));
  }
  function deleted(contextId: string) {
    ++chatRead.current;
    setChats((rows) => rows.filter((row) => row.context_id !== contextId));
    setSelected(null);
    setDirty(false);
    setNewKey((key) => key + 1);
    void refreshChats().catch((e) => setError(errorText(e)));
  }
  return (
    <div
      className="workspace"
      onKeyDown={(event) => {
        if (event.key === "Escape" && menu) {
          setMenu(false);
          menuButton.current?.focus();
        }
      }}
    >
      <a href="#main" className="skip-link">
        Перейти к содержимому
      </a>
      <button
        className="mobile-menu secondary"
        ref={menuButton}
        aria-expanded={menu}
        aria-controls="sidebar"
        aria-label={menu ? "Закрыть меню" : "Открыть меню"}
        onClick={() => setMenu(!menu)}
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          aria-hidden="true"
        >
          <rect x="3" y="4" width="18" height="16" rx="3" />
          <path d="M9 4v16" />
        </svg>
      </button>
      <aside className={`sidebar ${menu ? "open" : ""}`} id="sidebar">
        <button className="new-chat secondary" onClick={() => choose(null)}>
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            aria-hidden="true"
          >
            <path d="M12 5H6a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-6" />
            <path d="m16 4 4 4M10 14l1-4 8-8 4 4-8 8-5 1Z" />
          </svg>
          Новый чат
        </button>
        <div className="sidebar-chats">
          <h2 className="sidebar-label">Чаты</h2>
          {loading && (
            <p className="muted" role="status">
              Загружаем…
            </p>
          )}
          {error && (
            <div>
              <p className="error" role="alert">
                {error}
              </p>
              <button
                className="text-button"
                onClick={() =>
                  void refreshChats().catch((e) => setError(errorText(e)))
                }
              >
                Повторить
              </button>
            </div>
          )}
          {!loading && !chats.length && !error && (
            <p className="muted">Ваш первый чат появится здесь.</p>
          )}
          <div className="chat-list">
            {chats.map((row) => (
              <button
                className={`chat-link ${selected?.context_id === row.context_id && page === "chats" ? "selected" : ""}`}
                key={row.context_id}
                aria-current={
                  selected?.context_id === row.context_id && page === "chats"
                    ? "page"
                    : undefined
                }
                title={row.title || "Новый чат"}
                onClick={() => choose(row)}
              >
                <span className="chat-link-content">
                  <strong>{row.title || "Новый чат"}</strong>
                  {(row.needs_attention || row.status === "TASK_STATE_INPUT_REQUIRED") && <small className="chat-attention">Нужен ваш ответ</small>}
                  {["TASK_STATE_FAILED", "TASK_STATE_REJECTED"].includes(row.status ?? "") && <small className="chat-attention danger">Ошибка выполнения</small>}
                </span>
              </button>
            ))}
          </div>
        </div>
        <nav aria-label="Управление">
          {pages.map(([key, label]) => (
            <button
              key={key}
              className={`nav-item ${page === key ? "selected" : ""}`}
              aria-current={page === key ? "page" : undefined}
              onClick={() =>
                navigate(() => {
                  setPage(key);
                  setMenu(false);
                })
              }
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="profile">
          <span className="avatar" aria-hidden="true">
            {session.identity.actor_id.slice(0, 1).toUpperCase()}
          </span>
          <div>
            <strong>Владелец</strong>
            <small title={session.identity.actor_id}>
              {session.identity.actor_id}
            </small>
          </div>
          <button
            className="text-button"
            onClick={() => void session.logout().catch(() => {})}
          >
            Выйти
          </button>
        </div>
      </aside>
      <main id="main" tabIndex={-1}>
        {page === "chats" ? (
          <Chat
            key={selected ? selected.context_id : `new-${newKey}`}
            api={api}
            row={selectedRow}
            onTask={submitted}
            onDirty={setDirty}
            onRenamed={renamed}
            onDeleted={deleted}
          />
        ) : page === "schedules" ? (
          <Schedules api={api} chats={chats} onChat={choose} refreshChats={refreshChats} onDirty={setDirty} />
        ) : page === "access" ? (
          <ExternalAccess api={api} />
        ) : page === "settings" ? (
          <Settings api={api} />
        ) : page === "tools" ? (
          <ToolPolicies api={api} />
        ) : (
          <Agents api={api} />
        )}
      </main>
    </div>
  );
}
