import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Api, ApiError, errorText } from "./api";
import { History, useChatHistory } from "./History";
import { WorkspaceFiles } from "./WorkspaceFiles";
import {
  remoteProgress,
  remoteStates,
  states,
  terminal,
  textParts,
} from "./types";
import type { ChatRow, Message, Task } from "./types";

export function Chat({
  api,
  row,
  onTask,
  onDirty,
}: {
  api: Api;
  row: ChatRow | null;
  onTask: (task: Task) => void;
  onDirty: (value: boolean) => void;
}) {
  const [task, setTask] = useState<Task>();
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<{
    message: Message;
    configuration: { returnImmediately: true };
  }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [directAnswer, setDirectAnswer] = useState("");
  const [filesOpen, setFilesOpen] = useState(false);
  const [filesDirty, setFilesDirty] = useState(false);
  const closeFiles = () => {
    if (
      filesDirty &&
      !window.confirm(
        "Результат удаления ещё не подтверждён. Закрыть панель и потерять локальные данные для проверки и повтора запроса?",
      )
    )
      return;
    setFilesOpen(false);
    filesButton.current?.focus();
  };
  const filesButton = useRef<HTMLButtonElement>(null);
  const mounted = useRef(true);
  const observedRoot = useRef(row?.latest_task_id);
  const view = useRef({
    taskId: row?.latest_task_id,
    revision: 0,
    terminal: false,
  });
  const pendingRevision = useRef(0);
  const thread = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const contextId = row?.context_id ?? task?.contextId;
  const history = useChatHistory(api, contextId);
  const loadHistory = history.load;
  useLayoutEffect(() => {
    if (row?.latest_task_id !== observedRoot.current) {
      observedRoot.current = row?.latest_task_id;
      if (row?.latest_task_id && view.current.taskId !== row.latest_task_id) {
        view.current = {
          taskId: row.latest_task_id,
          revision: view.current.revision + 1,
          terminal: false,
        };
        setTask(undefined);
        setCancelConfirm(false);
      }
    }
  }, [row?.latest_task_id]);
  const acceptTask = useCallback((next: Task) => {
    if (
      view.current.taskId === next.id &&
      view.current.terminal &&
      !terminal(next)
    )
      return;
    if (view.current.taskId !== next.id) {
      view.current.revision++;
      setCancelConfirm(false);
    }
    view.current.taskId = next.id;
    view.current.terminal = terminal(next);
    setTask(next);
  }, []);
  useEffect(() => {
    if (stickToBottom.current && thread.current)
      thread.current.scrollTop = thread.current.scrollHeight;
  }, [history.page.items, history.waits]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const dirty = !!draft || !!pending || busy || filesDirty;
    onDirty(dirty);
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    if (dirty) window.addEventListener("beforeunload", warn);
    return () => {
      window.removeEventListener("beforeunload", warn);
      onDirty(false);
    };
  }, [draft, pending, busy, filesDirty, onDirty]);
  const taskId = task?.id ?? row?.latest_task_id;
  const refresh = useCallback(
    async (signal?: AbortSignal, reloadHistory = false) => {
      if (!taskId || !mounted.current || view.current.taskId !== taskId)
        return undefined;
      const revision = view.current.revision;
      const latest = await api.json<Task>(
        `/a2a/owner/tasks/${encodeURIComponent(taskId)}`,
        "GET",
        undefined,
        signal,
      );
      if (
        !signal?.aborted &&
        mounted.current &&
        revision === view.current.revision &&
        view.current.taskId === taskId
      ) {
        acceptTask(latest);
        await loadHistory(reloadHistory ? "reload" : "refresh", signal);
      }
      return latest;
    },
    [api, taskId, loadHistory, acceptTask],
  );
  useEffect(() => {
    if (!taskId) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function follow() {
      while (!abort.signal.aborted) {
        try {
          const latest = await refresh(abort.signal);
          if (abort.signal.aborted) return;
          setConnected(true);
          if (terminal(latest)) return;
          await api.subscribe(
            taskId!,
            async () => {
              await refresh(abort.signal);
            },
            abort.signal,
          );
        } catch (failure) {
          if (abort.signal.aborted || !api.session.valid) return;
          setConnected(false);
          if (
            failure instanceof ApiError &&
            [403, 404].includes(failure.status)
          ) {
            setError(errorText(failure));
            return;
          }
        }
        if (!abort.signal.aborted)
          await new Promise<void>((resolve) => {
            timer = setTimeout(resolve, 3000);
            abort.signal.addEventListener(
              "abort",
              () => {
                clearTimeout(timer);
                resolve();
              },
              { once: true },
            );
          });
      }
    }
    void follow();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [api, taskId, refresh]);
  // Re-render deadlines even when the peer's stream is quiet.
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  async function send() {
    if (busy || (!draft.trim() && !pending) || (!pending && row?.latest_task_id && !task))
      return;
    const body = pending ?? {
      message: {
        role: "ROLE_USER",
        messageId: crypto.randomUUID(),
        parts: [{ text: draft }],
        ...(row?.context_id ? { contextId: row.context_id } : {}),
        ...(task && !terminal(task) ? { taskId: task.id } : {}),
      },
      configuration: { returnImmediately: true as const },
    };
    if (!pending) pendingRevision.current = view.current.revision;
    const revision = pendingRevision.current;
    setPending(body);
    setBusy(true);
    setError("");
    try {
      const result = await api.json<{ task?: Task; message?: Message }>(
        "/a2a/owner/message:send",
        "POST",
        body,
      );
      if (!mounted.current) return;
      if (!result.task && !result.message) throw new Error("Missing result");
      setDraft("");
      setPending(undefined);
      stickToBottom.current = true;
      if (revision !== view.current.revision) {
        await loadHistory("refresh");
        return;
      }
      if (result.task) {
        acceptTask(result.task);
        onTask(result.task);
        if (result.task.id === taskId) await refresh();
      } else if (result.message)
        setDirectAnswer(textParts(result.message.parts));
    } catch (failure) {
      if (!mounted.current) return;
      if (
        failure instanceof ApiError &&
        failure.status >= 400 &&
        failure.status < 500 &&
        failure.status !== 408
      ) {
        setPending(undefined);
        setError(errorText(failure));
        await refresh().catch(() => {});
      } else
        setError(
          "Не удалось подтвердить приём сообщения. Повторите тот же запрос: идентификатор сохранён, новая задача не создаётся автоматически.",
        );
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!task) return;
    setBusy(true);
    setError("");
    try {
      await api.json(
        `/a2a/owner/tasks/${encodeURIComponent(task.id)}:cancel`,
        "POST",
      );
      if (mounted.current) {
        setCancelConfirm(false);
        await refresh();
      }
    } catch (failure) {
      if (mounted.current) setError(errorText(failure));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <section className="conversation" aria-label="Чат">
      <header className="chat-heading">
        <div>
          <h1>Core Agent</h1>
        </div>
        <div className="chat-heading-actions">
          {task && (
            <div className="task-status">
              <span
                className={`status-dot ${terminal(task) ? "" : "green"}`}
                aria-hidden="true"
              />
              {states[task.status.state] ?? "Состояние обновляется"}
            </div>
          )}
          {contextId && (
            <button
              ref={filesButton}
              className="secondary"
              aria-expanded={filesOpen}
              aria-controls="workspace-files"
              onClick={() => (filesOpen ? closeFiles() : setFilesOpen(true))}
            >
              Файлы
            </button>
          )}
        </div>
      </header>
      {filesOpen && contextId && (
        <WorkspaceFiles
          key={contextId}
          api={api}
          contextId={contextId}
          active={task ? !terminal(task) : !!row?.active}
          onDirty={setFilesDirty}
          onClose={closeFiles}
        />
      )}
      <div
        className="thread"
        ref={thread}
        onScroll={() => {
          if (thread.current)
            stickToBottom.current =
              thread.current.scrollHeight -
                thread.current.scrollTop -
                thread.current.clientHeight <
              100;
        }}
        aria-live="polite"
        aria-relevant="additions text"
      >
        {remoteProgress(task).map((entry) => (
          <p className="history-note" key={entry.task_id}>
            <strong>{entry.agent_name}</strong> ·{" "}
            {remoteStates[entry.remote_state]}
          </p>
        ))}
        {!task && !row && (
          <div className="welcome">
            <h2>Чем помочь?</h2>
            <p>Напишите сообщение, чтобы начать.</p>
          </div>
        )}
        {row?.latest_task_id && !task && !error && <p role="status">Загружаем задачу…</p>}
        <History
          api={api}
          history={history}
          onOlder={() => {
            stickToBottom.current = false;
          }}
          refresh={async () => {
            await refresh(undefined, true);
          }}
        />
        {directAnswer && (
          <article className="message result">
            <div className="eyebrow">Core Agent</div>
            <p className="prose">{directAnswer}</p>
          </article>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </div>
      <footer className="composer-wrap">
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
          <label className="sr-only" htmlFor="message">
            Сообщение агенту
          </label>
          <textarea
            id="message"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
            placeholder={
              task && !terminal(task)
                ? "Добавьте уточнение к текущей задаче…"
                : "Опишите задачу…"
            }
            disabled={busy || !!pending}
            rows={2}
          />
          <div className="composer-actions">
            <span className="muted">Shift+Enter — новая строка</span>
            <button
              disabled={
                busy || (!pending && (!draft.trim() || (!!row?.latest_task_id && !task)))
              }
            >
              {busy
                ? "Отправляем…"
                : pending
                  ? "Повторить тот же запрос"
                  : "Отправить ↑"}
            </button>
          </div>
        </form>
        <div className="connection-line">
          <span>
            {taskId
              ? connected
                ? "Состояние синхронизировано"
                : "Переподключаемся…"
              : "Решения и ответы сохраняются в этом чате"}
          </span>
          {task && !terminal(task) && (
            <div>
              {cancelConfirm ? (
                <>
                  <button
                    className="text-button danger"
                    disabled={busy}
                    onClick={() => void cancel()}
                  >
                    Подтвердить отмену
                  </button>
                  <button
                    className="text-button"
                    onClick={() => setCancelConfirm(false)}
                  >
                    Продолжить работу
                  </button>
                </>
              ) : (
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() => setCancelConfirm(true)}
                >
                  Отменить задачу
                </button>
              )}
            </div>
          )}
        </div>
      </footer>
    </section>
  );
}
