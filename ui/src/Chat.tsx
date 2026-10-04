import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Api, ApiError, errorText } from "./api";
import { Attachments, ConversationFiles, History, pendingAction, useChatHistory } from "./History";
import { actionLabel } from "./toolPresentation";
import { WorkspaceFiles } from "./WorkspaceFiles";
import { Markdown } from "./Markdown";
import { chatStreamEvent, nextLiveAnswer } from "./chatStream";
import { PeerConversationCard, PeerConversationPanel, peerOperationId, usePeerConversations } from "./PeerConversations";
import type { PeerConversation } from "./PeerConversations";
import { historyEntries } from "./ActionCard";
import {
  remoteProgress,
  formatFileSize,
  fileReceipt,
  terminal,
  textParts,
} from "./types";
import type { ChatRow, FileReceipt, LiveAnswerSnapshot, Message, Part, Settings, Task } from "./types";

// App remounts the first submitted chat when it receives its context ID.
const liveHandoff = new WeakMap<Api, string>();

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
  onRenamed,
  onDeleted,
}: {
  api: Api;
  row: ChatRow | null;
  onTask: (task: Task) => void;
  onDirty: (value: boolean) => void;
  onRenamed: (metadata: Partial<ChatRow> & { context_id: string }) => void;
  onDeleted: (contextId: string) => void;
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
  const [reconnecting, setReconnecting] = useState(false);
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [directAnswer, setDirectAnswer] = useState("");
  const [liveAnswer, setLiveAnswer] = useState<LiveAnswerSnapshot>();
  const liveTask = useRef<{ api: Api; id: string } | undefined>(
    row?.latest_task_id && liveHandoff.get(api) === row.latest_task_id
      ? { api, id: row.latest_task_id } : undefined,
  );
  const [filesOpen, setFilesOpen] = useState(false);
  const [peerSelection, setPeerSelection] = useState<string>();
  const peerTrigger = useRef<HTMLButtonElement | null>(null);
  const peerButton = useRef<HTMLButtonElement>(null);
  const [filesDirty, setFilesDirty] = useState(false);
  const [newMessages, setNewMessages] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleBusy, setTitleBusy] = useState(false);
  const [titleError, setTitleError] = useState("");
  const [copyNotice, setCopyNotice] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const deleteDialog = useRef<HTMLDialogElement>(null);
  const deleteRequest = useRef<AbortController | null>(null);
  const titleRequest = useRef<AbortController | null>(null);
  const titleEditRevision = useRef(0);
  const titleMenu = useRef<HTMLDetailsElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const olderAnchor = useRef<{ height: number; top: number } | null>(null);
  const historySignature = useRef("");
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
  const taskId = task?.id ?? row?.latest_task_id;
  const peerHistory = usePeerConversations(api, contextId, task ? !terminal(task) : !!row?.active);
  function openPeer(value: PeerConversation, trigger: HTMLButtonElement) {
    if (filesDirty) return;
    setFilesOpen(false);
    peerTrigger.current = trigger;
    setPeerSelection(value.operation_id);
  }
  function closePeer() {
    setPeerSelection(undefined);
    requestAnimationFrame(() => {
      const trigger = peerTrigger.current;
      if (trigger?.isConnected) trigger.focus(); else peerButton.current?.focus();
    });
  }
  useLayoutEffect(() => { liveHandoff.delete(api); }, [api]);
  useLayoutEffect(() => {
    const dialog = deleteDialog.current;
    if (deleteConfirm && dialog && !dialog.open) dialog.showModal();
    else if (!deleteConfirm && dialog?.open) dialog.close();
  }, [deleteConfirm]);
  const history = useChatHistory(api, contextId);
  const linkedPeers = new Set(historyEntries(history.page.items).map((entry) => peerOperationId(entry.action)));
  const unlinkedPeers = peerHistory.conversations.filter((value) => !linkedPeers.has(value.operation_id));
  const loadHistory = history.load;
  const liveText = liveTask.current?.api === api && liveTask.current.id === taskId
    && liveAnswer && liveAnswer.taskId === taskId && !liveAnswer.superseded
    && (!terminal(task) || task?.status.state === "TASK_STATE_COMPLETED")
    && !history.page.items.some((item) => item.task_id === taskId && (item.outcome
      || (item.kind === "agent_message" && item.status === "available" && item.text === liveAnswer.text)))
    ? liveAnswer.text : "";
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
        liveTask.current = undefined;
        setLiveAnswer(undefined);
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
      liveTask.current = undefined;
      setLiveAnswer(undefined);
    }
    view.current.taskId = next.id;
    view.current.terminal = terminal(next);
    setTask(next);
  }, []);
  useLayoutEffect(() => {
    const element = thread.current;
    if (!element) return;
    const signature = JSON.stringify([history.page.items, history.waits, directAnswer, liveText]);
    if (olderAnchor.current) {
      element.scrollTop = olderAnchor.current.top + element.scrollHeight - olderAnchor.current.height;
      olderAnchor.current = null;
    } else if (stickToBottom.current) {
      element.scrollTop = element.scrollHeight;
      setNewMessages(false);
    } else if (signature !== historySignature.current) setNewMessages(true);
    historySignature.current = signature;
  }, [history.page.items, history.waits, directAnswer, liveText]);
  useLayoutEffect(() => {
    const input = composerInput.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * .25)}px`;
  }, [draft]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      fileReader.current?.abort();
      titleRequest.current?.abort();
      deleteRequest.current?.abort();
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
    const dirty = !!draft || attachments.length > 0 || !!pending || busy || filesDirty || editingTitle;
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
  }, [draft, attachments, pending, busy, filesDirty, editingTitle, onDirty]);
  const attachmentBytes = attachments.reduce((sum, file) => sum + file.size, 0);
  const acceptedReceipt = acceptedFiles ?? fileReceipt(task?.metadata?.file_receipt);
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
    setReconnecting(false);
    if (!taskId) return;
    const abort = new AbortController();
    const revision = view.current.revision;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function follow() {
      while (!abort.signal.aborted) {
        try {
          const latest = await refresh(abort.signal);
          if (abort.signal.aborted) return;
          setReconnecting(false);
          if (terminal(latest)) return;
          try {
            await api.subscribe(
              taskId!,
              async (payload) => {
                if (abort.signal.aborted || !mounted.current || !api.session.valid
                  || view.current.taskId !== taskId || view.current.revision !== revision) return;
                const event = chatStreamEvent(payload, taskId!, contextId);
                if (!event) return;
                if (event.kind === "partial") {
                  if (event.snapshot && !view.current.terminal
                    && liveTask.current?.api === api && liveTask.current.id === taskId) {
                    const snapshot = event.snapshot;
                    setLiveAnswer((current) => view.current.taskId === taskId && !view.current.terminal
                      && liveTask.current?.api === api && liveTask.current.id === taskId
                      ? nextLiveAnswer(current, snapshot) : current);
                  }
                  return;
                }
                await refresh(abort.signal);
              },
              abort.signal,
            );
          } catch (failure) {
            // The canonical read below can keep the chat current without SSE.
            if (failure instanceof ApiError && [403, 404].includes(failure.status))
              throw failure;
          }
          if (abort.signal.aborted || !api.session.valid || view.current.terminal) return;
        } catch (failure) {
          if (abort.signal.aborted || !api.session.valid) return;
          if (
            failure instanceof ApiError &&
            [403, 404].includes(failure.status)
          ) {
            setReconnecting(false);
            setError(errorText(failure));
            return;
          }
          setReconnecting(true);
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
  }, [api, taskId, contextId, refresh]);
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
        liveTask.current = { api, id: result.task.id };
        if (!row?.context_id) liveHandoff.set(api, result.task.id);
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
  async function archive() {
    if (!contextId || busy || pending || filesDirty || (taskId && (!task || !terminal(task)))) return;
    const abort = new AbortController();
    deleteRequest.current = abort;
    setBusy(true);
    setDeleteError("");
    try {
      const receipt = await api.json<{ context_id: string; archived: boolean }>(
        `/api/chats/${encodeURIComponent(contextId)}`, "DELETE", undefined, abort.signal,
      );
      if (receipt.context_id !== contextId || receipt.archived !== true) throw new Error("Invalid deletion receipt");
      if (!abort.signal.aborted && mounted.current && api.session.valid) onDeleted(contextId);
    } catch (failure) {
      if (!abort.signal.aborted && mounted.current) {
        setDeleteError(failure instanceof ApiError && failure.code === "CONTEXT_BUSY"
          ? "В чате выполняется задача. Закройте это окно и остановите её или дождитесь завершения."
          : errorText(failure));
      }
    } finally {
      if (!abort.signal.aborted && mounted.current) setBusy(false);
    }
  }
  async function rename() {
    if (!contextId || titleBusy || !titleDraft.trim() || Array.from(titleDraft.trim()).length > 120) return;
    const abort = new AbortController();
    titleRequest.current = abort;
    setTitleBusy(true);
    setTitleError("");
    try {
      const metadata = await api.json<Partial<ChatRow> & { context_id: string }>(
        `/api/chats/${encodeURIComponent(contextId)}/title`, "PUT",
        { title: titleDraft.trim(), expected_revision: titleEditRevision.current }, abort.signal,
      );
      if (!abort.signal.aborted && mounted.current && api.session.valid) {
        onRenamed(metadata);
        setEditingTitle(false);
      }
    } catch (failure) {
      if (!abort.signal.aborted && mounted.current) {
        setTitleError(errorText(failure));
        if (failure instanceof ApiError && failure.status === 409) {
          onRenamed({ context_id: contextId });
          setEditingTitle(false);
          setError("Название уже изменено другим владельцем. Откройте переименование ещё раз, чтобы проверить актуальное значение.");
        }
      }
    } finally {
      if (!abort.signal.aborted && mounted.current) setTitleBusy(false);
    }
  }
  function jump(id: string) {
    const element = document.getElementById(id);
    if (!element) return;
    stickToBottom.current = false;
    element.scrollIntoView({ block: "center", behavior: "smooth" });
    element.setAttribute("tabindex", "-1");
    element.focus({ preventScroll: true });
  }
  const activeWait = (!terminal(task) && taskId ? history.waits[taskId] : undefined)?.find((item) => !item.outcome);
  const currentAction = taskId ? pendingAction(history.page.items, taskId) : undefined;
  const peers = remoteProgress(task);
  const latestOutcome = history.page.items.find((item) => item.task_id === taskId && item.kind === "result")?.outcome;
  const timerUntil = currentAction?.name === "core_wait_until" && currentAction.args && typeof currentAction.args === "object"
    ? (currentAction.args as Record<string, unknown>).until : undefined;
  const status = !task ? (row?.latest_task_id ? "Загружаем текущий запрос…" : "Готов к новому запросу")
    : activeWait ? activeWait.kind === "owner_question" ? "Ожидает вашего ответа" : "Ожидает вашего разрешения"
    : task.status.state === "TASK_STATE_COMPLETED" ? latestOutcome?.complete === false ? "Работа завершена частично" : "Ответ подготовлен"
    : task.status.state === "TASK_STATE_FAILED" ? "Не удалось завершить запрос"
    : task.status.state === "TASK_STATE_CANCELED" ? "Запрос остановлен"
    : task.status.state === "TASK_STATE_REJECTED" ? "Запрос отклонён"
    : peers.length ? `Ожидает внешнего агента · ${peers[0].agent_name}`
    : typeof timerUntil === "string" && Number.isFinite(Date.parse(timerUntil)) ? `Ожидает до ${new Date(timerUntil).toLocaleString("ru-RU")}`
    : currentAction ? actionLabel(currentAction.name, currentAction.args)
    : task.status.state === "TASK_STATE_INPUT_REQUIRED" ? "Ожидает вашего ответа"
    : task.status.state === "TASK_STATE_AUTH_REQUIRED" ? "Требуется авторизация"
    : task.status.state === "TASK_STATE_SUBMITTED" ? "Запрос принят" : "Готовит ответ";
  const fileCount = history.page.items.reduce((count, item) => count + (item.attachments?.length ?? 0) + (item.response_files?.length ?? 0), 0);
  return (
    <section className={`conversation ${peerSelection ? "peer-panel-open" : ""}`} aria-label="Чат">
      <header className="chat-heading">
        <div className="chat-title">
          <h1>{row?.title || "Новый чат"}</h1>
          <p className="task-status" role="status" aria-live="polite" aria-atomic="true">{status}</p>
        </div>
        <div className="chat-heading-actions">
          {contextId && <button className="text-button history-refresh" disabled={history.loading}
            title="Обновить историю" aria-label="Обновить историю" onClick={() => void (taskId ? refresh(undefined, true) : loadHistory("reload")).catch((failure) => setError(errorText(failure)))}>
            <span aria-hidden="true">↻</span><span className="sr-only">Обновить историю</span>
          </button>}
          {contextId && (
            <button
              ref={filesButton}
              className="secondary"
              aria-expanded={filesOpen}
              aria-controls="conversation-files-panel"
              onClick={() => {
                if (filesOpen) closeFiles(); else { setPeerSelection(undefined); setFilesOpen(true); }
              }}
            >
              Файлы <span className="file-count">{fileCount}{history.page.next_cursor ? "+" : ""}</span>
            </button>
          )}
          {!!peerHistory.conversations.length && <button ref={peerButton} className="secondary peer-panel-toggle"
            disabled={filesDirty} aria-expanded={!!peerSelection} aria-controls="peer-conversation-panel"
            onClick={(event) => peerSelection ? closePeer() : openPeer(peerHistory.conversations[0], event.currentTarget)}>
            Переписка агентов <span className="file-count">{peerHistory.conversations.length}</span>
          </button>}
          {contextId && <details className="chat-menu" ref={titleMenu}>
            <summary aria-label="Действия с чатом">⋯</summary>
            <div className="chat-menu-actions">
              <button className="text-button" onClick={() => { titleEditRevision.current = row?.title_revision ?? 0; setTitleDraft(row?.title ?? ""); setEditingTitle(true); setTitleError(""); if (titleMenu.current) titleMenu.current.open = false; }}>Переименовать</button>
              <button className="text-button" onClick={() => void navigator.clipboard.writeText(contextId).then(() => setCopyNotice("ID чата скопирован"), () => setCopyNotice("Не удалось скопировать ID чата"))}>Скопировать ID</button>
              <button className="text-button danger" disabled={busy || !!pending || filesDirty || titleBusy || !!taskId && (!task || !terminal(task))}
                title={taskId && (!task || !terminal(task)) ? "Сначала остановите текущую задачу или дождитесь её завершения" : undefined}
                onClick={() => { setDeleteError(""); setDeleteConfirm(true); if (titleMenu.current) titleMenu.current.open = false; }}>Удалить чат</button>
              {copyNotice && <p className="muted" role="status">{copyNotice}</p>}
            </div>
          </details>}
        </div>
      </header>
      {peerSelection && contextId && <PeerConversationPanel api={api} contextId={contextId} selected={peerSelection}
        conversations={peerHistory.conversations} onSelect={setPeerSelection} onClose={closePeer} />}
      {contextId && <dialog ref={deleteDialog} className="chat-delete-dialog" aria-labelledby="chat-delete-title" aria-describedby="chat-delete-description"
        onClose={() => titleMenu.current?.querySelector<HTMLElement>("summary")?.focus()}
        onCancel={(event) => { if (busy) event.preventDefault(); else setDeleteConfirm(false); }}>
        <h2 id="chat-delete-title">Удалить чат?</h2>
        <p id="chat-delete-description">Чат исчезнет из списка у всех владельцев. Его расписания отключатся. История, файлы и прежние результаты A2A сохранятся.</p>
        {(draft || attachments.length > 0) && <p>Неотправленное сообщение и выбранные вложения будут сброшены.</p>}
        {deleteError && <p className="error" role="alert">{deleteError}</p>}
        <div className="actions"><button type="button" className="secondary" autoFocus disabled={busy} onClick={() => setDeleteConfirm(false)}>Отмена</button>
          <button type="button" className="danger" disabled={busy || !!taskId && (!task || !terminal(task))} onClick={() => void archive()}>{busy ? "Удаляем…" : "Подтвердить удаление чата"}</button></div>
      </dialog>}
      {editingTitle && <form className="chat-title-form" onSubmit={(event) => { event.preventDefault(); void rename(); }}>
        <label>Название чата<input autoFocus maxLength={240} value={titleDraft} disabled={titleBusy} onChange={(event) => setTitleDraft(event.target.value)} /></label>
        <div className="actions"><button disabled={titleBusy || !titleDraft.trim() || Array.from(titleDraft.trim()).length > 120}>{titleBusy ? "Сохраняем…" : "Сохранить название"}</button>
          <button type="button" className="text-button" disabled={titleBusy} onClick={() => setEditingTitle(false)}>Отмена</button></div>
        {Array.from(titleDraft.trim()).length > 120 && <p className="error" role="alert">Название должно содержать не более 120 символов.</p>}
        {titleError && <p className="error" role="alert">{titleError}</p>}
      </form>}
      {filesOpen && contextId && (
        <aside className="conversation-files-panel" id="conversation-files-panel" aria-label="Файлы чата">
          <div className="section-line"><h2>Файлы чата</h2><button className="text-button" aria-label="Закрыть файлы чата" onClick={closeFiles}>×</button></div>
          <ConversationFiles api={api} contextId={contextId} items={history.page.items} onJump={(id) => {
            if (filesDirty) return;
            setFilesOpen(false);
            jump(`history-${id}`);
          }} />
          {history.page.next_cursor && <button className="text-button" disabled={history.loading} onClick={() => void history.load("older")}>Загрузить файлы предыдущих сообщений</button>}
          <details className="file-cleanup"><summary>Очистка файлов рабочего пространства</summary>
            <WorkspaceFiles key={contextId} api={api} contextId={contextId} active={task ? !terminal(task) : !!row?.active} onDirty={setFilesDirty} onClose={closeFiles} />
          </details>
        </aside>
      )}
      <div
        className="thread"
        ref={thread}
        onScroll={() => {
          if (thread.current) {
            stickToBottom.current =
              thread.current.scrollHeight -
                thread.current.scrollTop -
                thread.current.clientHeight <
              100;
            if (stickToBottom.current) setNewMessages(false);
          }
        }}
        onFocusCapture={(event) => {
          const bounds = event.target.getBoundingClientRect();
          const container = thread.current?.getBoundingClientRect();
          if (container && (bounds.bottom > container.bottom || bounds.top < container.top)) {
            stickToBottom.current = false;
            event.target.scrollIntoView({ block: "nearest" });
          }
        }}
      >
        {unlinkedPeers.map((entry) => <PeerConversationCard key={entry.operation_id} conversation={entry} onOpen={openPeer} />)}
        {peerHistory.error && <p className="muted peer-list-error" role="status">Не удалось обновить список обращений к агентам.</p>}
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
          peerConversations={peerHistory.conversations}
          onPeerOpen={openPeer}
          onOlder={() => {
            stickToBottom.current = false;
            if (thread.current) olderAnchor.current = { height: thread.current.scrollHeight, top: thread.current.scrollTop };
          }}
          refresh={async () => {
            await refresh(undefined, true);
          }}
        />
        {liveText && <article className="message live-answer" aria-label="Предварительный ответ">
          <p className="muted" role="status">{terminal(task)
            ? "Ответ подготовлен. Загружаем сохранённый результат…" : "Предварительный ответ · формируется"}</p>
          <div className="prose">{liveText}</div>
        </article>}
        {directAnswer && (
          <article className="message result">
            <Markdown text={directAnswer} />
          </article>
        )}
        {acceptedReceipt && !history.page.items.some((item) => item.attachments?.some((entry) => acceptedReceipt.entries.some((accepted) => accepted.relative_path === entry.relative_path))) && <div className="accepted-attachments" role="status">
          <p className="muted">Файлы переданы агенту</p>
          <Attachments entries={acceptedReceipt.entries} api={api} contextId={contextId} />
        </div>}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </div>
      <footer className="composer-wrap">
        {newMessages && <button className="new-messages secondary" onClick={() => { stickToBottom.current = true; if (thread.current) thread.current.scrollTop = thread.current.scrollHeight; setNewMessages(false); }}>Новые сообщения ↓</button>}
        {activeWait && <div className="pending-interaction-bar"><span>{activeWait.kind === "owner_question" ? "Агенту нужен ваш ответ" : "Требуется ваше разрешение"}</span><button className="text-button" onClick={() => jump(`interaction-${activeWait.wait_id}`)}>Перейти к запросу</button></div>}
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
              <span className="muted">{formatFileSize(file.size)}</span>
              <button type="button" className="text-button" disabled={busy || !!pending}
                aria-label={`Удалить ${file.name} из вложений`}
                onClick={() => { setAttachments((current) => current.filter((_, position) => position !== index)); setError(""); }}>×</button>
            </li>)}
          </ul>}
          <label className="sr-only" htmlFor="message">
            Сообщение агенту
          </label>
          <textarea
            ref={composerInput}
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
              <span className={`attachment-summary muted ${attachmentBytes > attachmentLimit ? "attachment-limit" : ""}`} id="attachment-summary" aria-live="polite">
                {attachments.length > 0 && `${attachments.length} файла · ${formatFileSize(attachmentBytes)} / `}
                До {formatFileSize(attachmentLimit)} суммарно{!limitConfirmed && " по умолчанию"}
              </span>
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
          <span role="status">{taskId && reconnecting ? "Соединение потеряно. Восстанавливаем…" : ""}</span>
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
                    Остановить
                </button>
              )}
            </div>
          )}
        </div>
      </footer>
    </section>
  );
}
