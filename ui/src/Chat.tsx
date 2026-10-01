import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Api, ApiError, errorText } from "./api";
import { Attachments, History, useChatHistory } from "./History";
import { WorkspaceFiles } from "./WorkspaceFiles";
import {
  remoteProgress,
  byteCount,
  fileReceipt,
  remoteStates,
  states,
  terminal,
  textParts,
} from "./types";
import type { ChatRow, FileReceipt, Message, Part, Settings, Task } from "./types";

function settingsLimit(settings: Settings): number {
  const value = settings.attachment_limit_bytes;
  if (!Number.isSafeInteger(value) || value < 1 || value > 2_147_483_647) throw new Error("Invalid attachment limit");
  return value;
}

function readFile(file: File, reader: FileReader): Promise<Part> {
  return new Promise((resolve, reject) => {
    reader.onerror = reader.onabort = () => reject(new Error("File read failed"));
    reader.onload = () => {
      const result = reader.result;
      const comma = typeof result === "string" ? result.indexOf(",") : -1;
      if (typeof result !== "string" || comma < 0 || !result.slice(0, comma).endsWith(";base64")) {
        reject(new Error("Invalid file encoding"));
        return;
      }
      resolve({ raw: result.slice(comma + 1), filename: file.name, mediaType: file.type || "application/octet-stream" });
    };
    reader.readAsDataURL(file);
  });
}

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
  const [attachments, setAttachments] = useState<File[]>([]);
  const [attachmentLimit, setAttachmentLimit] = useState(25_000_000);
  const [limitConfirmed, setLimitConfirmed] = useState(false);
  const [acceptedFiles, setAcceptedFiles] = useState<FileReceipt>();
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
  const fileInput = useRef<HTMLInputElement>(null);
  const fileReader = useRef<FileReader>(null);
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
        setAcceptedFiles(undefined);
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
      setAcceptedFiles(undefined);
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
      fileReader.current?.abort();
    };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    void api.json<Settings>("/api/settings", "GET", undefined, abort.signal).then((settings) => {
      if (!abort.signal.aborted) {
        setAttachmentLimit(settingsLimit(settings));
        setLimitConfirmed(true);
      }
    }).catch(() => {});
    return () => abort.abort();
  }, [api]);
  useEffect(() => {
    const dirty = !!draft || attachments.length > 0 || !!pending || busy || filesDirty;
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
  }, [draft, attachments, pending, busy, filesDirty, onDirty]);
  const attachmentBytes = attachments.reduce((sum, file) => sum + file.size, 0);
  const acceptedReceipt = acceptedFiles ?? fileReceipt(task?.metadata?.file_receipt);
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
    if (busy || (!draft.trim() && !attachments.length && !pending) || (!pending && row?.latest_task_id && !task))
      return;
    const revision = pending ? pendingRevision.current : view.current.revision;
    const previousBatch = fileReceipt(task?.metadata?.file_receipt)?.batch_id;
    setBusy(true);
    setError("");
    try {
      let body = pending;
      if (!body) {
        const parts: Part[] = draft.trim() ? [{ text: draft }] : [];
        if (attachments.length) {
          let limit: number;
          try {
            limit = settingsLimit(await api.json<Settings>("/api/settings"));
            if (!mounted.current) return;
            setAttachmentLimit(limit);
            setLimitConfirmed(true);
          } catch {
            if (mounted.current) setError("Не удалось проверить лимит вложений. Сообщение и файлы сохранены. Повторите отправку.");
            return;
          }
          if (attachmentBytes > limit) {
            setError(errorText(new ApiError(413, "ATTACHMENTS_TOO_LARGE", { allowed_bytes: limit, actual_bytes: attachmentBytes })));
            return;
          }
          try {
            for (const file of attachments) {
              const reader = new FileReader();
              fileReader.current = reader;
              parts.push(await readFile(file, reader));
              if (!mounted.current) return;
            }
          } catch {
            if (mounted.current) setError("Не удалось прочитать вложения. Сообщение и выбранные файлы сохранены; отправка не началась.");
            return;
          } finally {
            fileReader.current = null;
          }
        }
        body = {
          message: {
            role: "ROLE_USER", messageId: crypto.randomUUID(), parts,
            ...(row?.context_id ? { contextId: row.context_id } : {}),
            ...(task && !terminal(task) ? { taskId: task.id } : {}),
          },
          configuration: { returnImmediately: true },
        };
        pendingRevision.current = revision;
        setPending(body);
      }
      const result = await api.json<{ task?: Task; message?: Message }>(
        "/a2a/owner/message:send",
        "POST",
        body,
      );
      if (!mounted.current) return;
      if (!result.task && !result.message) throw new Error("Missing result");
      setDraft("");
      setAttachments([]);
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
      const metadata = result.task?.metadata ?? result.message?.metadata;
      const receipt = fileReceipt(metadata?.accepted_message_id === body.message.messageId
        ? metadata.accepted_file_receipt : metadata?.file_receipt);
      if (receipt && receipt.batch_id !== previousBatch) setAcceptedFiles(receipt);
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
          contextId={contextId}
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
        {acceptedReceipt && <div className="accepted-attachments" role="status">
          <p className="muted">Вложения приняты · папка attachments/{acceptedReceipt.batch_id}</p>
          <Attachments entries={acceptedReceipt.entries} />
        </div>}
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
          <input ref={fileInput} type="file" multiple className="sr-only" tabIndex={-1}
            aria-label="Выбрать вложения" disabled={busy || !!pending}
            onChange={(event) => {
              if (!busy && !pending) {
                const chosen = Array.from(event.target.files ?? []);
                setAttachments((current) => [...current, ...chosen]);
                setError("");
              }
              event.target.value = "";
            }} />
          {attachments.length > 0 && <ul className="composer-attachments" aria-label="Выбранные вложения">
            {attachments.map((file, index) => <li key={index}>
              <span className="attachment-name" title={file.name}>{file.name}</span>
              <span className="muted">{byteCount(file.size)}</span>
              <button type="button" className="text-button" disabled={busy || !!pending}
                aria-label={`Удалить ${file.name} из вложений`}
                onClick={() => { setAttachments((current) => current.filter((_, position) => position !== index)); setError(""); }}>×</button>
            </li>)}
          </ul>}
          <p className={`attachment-summary muted ${attachmentBytes > attachmentLimit ? "attachment-limit" : ""}`} id="attachment-summary" aria-live="polite">
            {attachments.length > 0 && `Файлов: ${attachments.length} · ${byteCount(attachmentBytes)} · `}
            Лимит {byteCount(attachmentLimit)}{!limitConfirmed && " по умолчанию"}
          </p>
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
            <div className="composer-tools">
              <button type="button" className="secondary attach-button" aria-describedby="attachment-summary"
                disabled={busy || !!pending} onClick={() => fileInput.current?.click()}>Прикрепить файлы</button>
              <span className="muted">Shift+Enter — новая строка</span>
            </div>
            <button
              disabled={
                busy || (!pending && ((!draft.trim() && !attachments.length) || (!!row?.latest_task_id && !task)))
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
