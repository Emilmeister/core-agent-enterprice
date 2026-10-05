import { Markdown } from "./Markdown";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Api, errorText } from "./api";
import { scheduleTime } from "./Schedules";
import { InteractionCard } from "./Interactions";
import { mergeHistoryPage } from "./types";
import type { AttachmentEntry, HistoryItem, HistoryPage, Interaction } from "./types";
import { ActionCard, ActionDialog, actionState, historyEntries } from "./ActionCard";
import type { HistoryEntry } from "./ActionCard";
import { FileCard } from "./FileCard";
import { PeerConversationCard, peerOperationId } from "./PeerConversations";
import type { PeerConversation } from "./PeerConversations";
export { ConversationFiles, FileCard } from "./FileCard";
export { pendingAction } from "./ActionCard";

const empty: HistoryPage = { items: [], next_cursor: null };
const statusLabels: Record<HistoryItem["status"], string> = {
  available: "",
  queued: "Сообщение принято и ожидает обработки",
  pending_guardrail: "Обработка поручения",
  rejected: "Материал отклонён",
  timed_out: "Срок проверки истёк",
  unprocessed_due_to_failure: "Не обработано: задача завершилась с ошибкой",
  unprocessed_due_to_cancel: "Не обработано: задача отменена",
};
const outcomes: Record<string, string> = {
  COMPLETED: "Завершено",
  FAILED: "Ошибка",
  ABORTED: "Работа прервана",
  CANCELLED: "Отменено",
  REJECTED: "Отклонено",
};

export function useChatHistory(api: Api, contextId?: string) {
  const [page, setPage] = useState<HistoryPage>(empty);
  const [waits, setWaits] = useState<Record<string, Interaction[]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const current = useRef(empty);
  const cache = useRef(new Map<string, Interaction[]>());
  const lifetime = useRef<AbortController | undefined>(undefined);
  const queue = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    const abort = new AbortController();
    lifetime.current = abort;
    current.current = empty;
    cache.current.clear();
    setPage(empty);
    setWaits({});
    setError("");
    return () => abort.abort();
  }, [api, contextId]);

  const load = useCallback(
    (
      mode: "refresh" | "older" | "reload" = "refresh",
      signal?: AbortSignal,
    ) => {
      const life = lifetime.current;
      if (!contextId || !life) return Promise.resolve();
      const stopped = () =>
        life.signal.aborted || !!signal?.aborted || !api.session.valid;
      // Serialize head refresh and pagination so an older response cannot overwrite a newer page.
      const work = queue.current.then(async () => {
        if (stopped()) return;
        const before = current.current;
        if (mode === "older" && !before.next_cursor) return;
        setLoading(true);
        setError("");
        try {
          async function read(cursor: string | null) {
            const query = new URLSearchParams({ limit: "50" });
            if (cursor) query.set("cursor", cursor);
            const response = await api.json<HistoryPage>(
              `/api/chats/${encodeURIComponent(contextId!)}/history?${query}`,
              "GET",
              undefined,
              signal ?? life!.signal,
            );
            if (
              !Array.isArray(response.items) ||
              (response.next_cursor !== null &&
                typeof response.next_cursor !== "string") ||
              (cursor !== null && response.next_cursor === cursor)
            )
              throw new Error("Invalid history page");
            return response;
          }
          let next = await read(mode === "older" ? before.next_cursor : null);
          if (stopped()) return;
          if (
            mode === "reload" &&
            next.items.some((item) =>
              before.items.some((old) => old.id === item.id),
            )
          ) {
            // A decision can change an older placeholder. Refresh the visible window after it,
            // without turning every live event into a fetch of the whole transcript.
            const oldest = before.items.at(-1)?.id;
            const seen = new Set<string>();
            const pages = Math.ceil(before.items.length / 50) + 1;
            for (
              let index = 0;
              next.next_cursor &&
              index < pages &&
              !next.items.some((item) => item.id === oldest);
              index++
            ) {
              if (stopped()) return;
              const cursor = next.next_cursor;
              if (seen.has(cursor)) throw new Error("Repeated history cursor");
              seen.add(cursor);
              next = mergeHistoryPage(next, await read(cursor), true);
            }
          } else {
            next = mergeHistoryPage(before, next, mode === "older");
          }
          if (stopped()) return;
          current.current = next;
          setPage(next);
          const roots = new Set(next.items.flatMap((item) => item.task_id ? [item.task_id] : []));
          const finished = new Set(
            next.items
              .filter((item) => item.kind === "result")
              .map((item) => item.task_id),
          );
          const updated: Record<string, Interaction[]> = {};
          for (const root of roots) {
            let interactions = cache.current.get(root);
            if (
              mode === "reload" ||
              !interactions ||
              !finished.has(root) ||
              interactions.some((item) => !item.outcome)
            ) {
              interactions = await api.pages<Interaction>(
                `/api/interactions?task_id=${encodeURIComponent(root)}&status=all`,
                "interactions",
                signal ?? life.signal,
              );
            }
            if (stopped()) return;
            cache.current.set(root, interactions);
            updated[root] = interactions;
          }
          for (const root of cache.current.keys())
            if (!roots.has(root)) cache.current.delete(root);
          setWaits(updated);
        } catch (failure) {
          if (!stopped()) setError(errorText(failure));
        } finally {
          if (!stopped()) setLoading(false);
        }
      });
      queue.current = work.catch(() => {});
      return work;
    },
    [api, contextId],
  );
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      await load("refresh", abort.signal);
      if (!abort.signal.aborted) timer = setTimeout(poll, 15000);
    }
    void poll();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [load]);
  return { page, waits, loading, error, load };
}

export function Attachments({ entries, api, contextId }: { entries: AttachmentEntry[]; api?: Api; contextId?: string }) {
  return <ul className="message-attachments" aria-label="Принятые вложения">
    {entries.map((entry) => <FileCard key={entry.index} api={api} contextId={contextId} file={entry} />)}
  </ul>;
}

function Entry({ api, contextId, item }: { api: Api; contextId?: string; item: HistoryItem }) {
  if (item.kind === "schedule_notice") {
    const reasons: Record<string, string> = {
      context_busy: "чат был занят", late: "время запуска пропущено",
      service_unavailable: "сервис был недоступен",
      workspace_cleanup_pending: "ожидалось завершение очистки файлов",
    };
    const timezone = item.timezone ?? "Europe/Moscow";
    return <article className="schedule-notice" id={`history-${item.id}`} data-history-id={item.id}>
      <p>Запуск по расписанию пропущен: {reasons[item.reason ?? ""] ?? "запуск был недоступен"}.</p>
      <p className="muted">{scheduleTime(item.due_at, timezone)}{item.through ? " — " + scheduleTime(item.through, timezone) : ""} · {timezone}</p>
    </article>;
  }
  return (
    <article
      className={`message ${item.kind === "user_message" ? "from-owner" : ""} ${item.kind === "placeholder" ? "history-placeholder" : ""}`}
      id={`history-${item.id}`} data-history-id={item.id}
    >
      <>
          {item.kind === "placeholder" && <div className="eyebrow">Статус сообщения</div>}
          {item.kind !== "placeholder" && item.status === "available"
            && (item.display_text ?? item.text) && (item.kind === "user_message"
              ? <p className="prose">{item.display_text ?? item.text}</p>
              : <Markdown text={item.text} />)}
      </>
      {statusLabels[item.status] && (
        <p className="muted history-status">{statusLabels[item.status]}</p>
      )}
      {item.status === "available" && item.attachments?.length ? <Attachments entries={item.attachments} api={api} contextId={contextId} /> : null}
      {item.kind === "result" && item.status === "available" && item.outcome?.state === "COMPLETED"
        && contextId && item.task_id && item.response_files?.length ?
        <ul className="message-attachments response-files" aria-label="Файлы ответа">
          {item.response_files.map((file) => <FileCard key={`${contextId}:${item.task_id}:${file.file_id}`}
            api={api} contextId={contextId} taskId={item.task_id!} file={file} />)}
        </ul> : null}
      {item.outcome && (
        <p className="muted history-status">
          {outcomes[item.outcome.state] ?? "Задача завершена"}
          {item.outcome.complete === false && " · Неполный результат"}
          {item.outcome.error_code && ` · ${item.outcome.error_code}`}
        </p>
      )}
    </article>
  );
}

export function History({
  api,
  contextId,
  history,
  refresh,
  onOlder,
  peerConversations = [],
  onPeerOpen,
}: {
  api: Api;
  contextId?: string;
  history: ReturnType<typeof useChatHistory>;
  refresh: () => Promise<void>;
  onOlder: () => void;
  peerConversations?: PeerConversation[];
  onPeerOpen?: (value: PeerConversation, trigger: HTMLButtonElement) => void;
}) {
  const groups: { root: string | null; entries: HistoryEntry[] }[] = [];
  for (const entry of historyEntries(history.page.items)) {
    const previous = groups.at(-1);
    if (previous && previous.root === entry.item.task_id) previous.entries.push(entry);
    else groups.push({ root: entry.item.task_id, entries: [entry] });
  }
  const [selectedAction, setSelectedAction] = useState<string>();
  const selected = groups.flatMap((group) => group.entries).find((entry) => entry.action?.key === selectedAction)?.action;
  useEffect(() => { if (selectedAction && !selected) setSelectedAction(undefined); }, [selectedAction, selected]);
  function closeAction() {
    setSelectedAction(undefined);
    const card = document.querySelector<HTMLElement>(`[data-action-key="${CSS.escape(selectedAction ?? "")}"] > button`);
    const group = card?.closest<HTMLDetailsElement>("details:not([open])");
    (group?.querySelector<HTMLElement>("summary") ?? card)?.focus({ preventScroll: true });
  }
  const reviews = new Map(
    Object.values(history.waits)
      .flat()
      .map((wait) => [wait.wait_id, wait]),
  );
  const linked = new Set(
    history.page.items.flatMap((item) =>
      item.review ? [item.review.wait_id] : [],
    ),
  );
  const rendered = new Set<string>();
  return (
    <>
      {selected && <ActionDialog action={selected}
        terminal={history.page.items.find((item) => item.task_id === selected.taskId && item.outcome)}
        waits={selected.taskId ? history.waits[selected.taskId] ?? [] : []} onClose={closeAction} />}
      {history.page.next_cursor && (
        <div className="history-pagination">
            <button
              className="secondary"
              disabled={history.loading}
              onClick={() => {
                onOlder();
                void history.load("older");
              }}
            >
              {history.loading
                ? "Загружаем…"
                : "Загрузить предыдущие сообщения"}
            </button>
        </div>
      )}
      {history.loading && !history.page.items.length && (
        <p className="muted" role="status">
          Загружаем историю…
        </p>
      )}
      {history.error && (
        <div className="error" role="alert">
          {history.error}
          <p>Обновите историю кнопкой в шапке чата.</p>
        </div>
      )}
      {groups.map(({ root, entries }, index) => {
        const waits = root ? history.waits[root] ?? [] : [];
        const terminal = history.page.items.find((item) => item.task_id === root && item.outcome);
        const interactions = root && !groups.slice(index + 1).some((group) => group.root === root)
          ? waits : [];
        const blocks: HistoryEntry[][] = [];
        for (const entry of entries) {
          const previous = blocks.at(-1);
          if (entry.action && !peerOperationId(entry.action) && actionState(entry.action, terminal, waits).kind === "success"
            && previous?.at(-1)?.action
            && !peerOperationId(previous.at(-1)!.action)
            && actionState(previous.at(-1)!.action!, terminal, waits).kind === "success") previous.push(entry);
          else blocks.push([entry]);
        }
        function renderEntry({ item, action }: HistoryEntry) {
          const peer = onPeerOpen && peerConversations.find((value) => value.operation_id === peerOperationId(action));
          const review = item.review && !rendered.has(item.review.wait_id) ? reviews.get(item.review.wait_id) : undefined;
          if (review) rendered.add(review.wait_id);
          return <Fragment key={action?.key ?? item.id}>
            {action && peer && onPeerOpen ? <div className="peer-conversation-action" data-history-id={item.id}>
              <PeerConversationCard conversation={peer} onOpen={onPeerOpen} />
              <details className="peer-action-details"><summary>Технические данные поручения</summary>
                <ActionCard action={action} terminal={terminal} waits={waits} onOpen={() => setSelectedAction(action.key)} />
              </details>
            </div> : action ? <ActionCard action={action} terminal={terminal} waits={waits} onOpen={() => setSelectedAction(action.key)} />
              : <Entry api={api} contextId={contextId} item={item} />}
            {review && <InteractionCard key={review.wait_id} api={api} item={review} refresh={refresh} />}
          </Fragment>;
        }
        return (
          <section key={entries[0].action?.key ?? entries[0].item.id} className="history-run" aria-label="Обращение">
            {blocks.map((block) => block.length > 1
              ? <details className="execution-group" key={block[0].action!.key}>
                <summary>Ход выполнения · {block.length} действий выполнено</summary>
                <div className="execution-steps" tabIndex={0} role="region" aria-label="Выполненные действия">
                  {block.map(renderEntry)}
                </div>
              </details> : renderEntry(block[0]))}
            {interactions
              .filter((item) => !linked.has(item.wait_id))
              .map((item) => (
                <InteractionCard
                  key={item.wait_id}
                  api={api}
                  item={item}
                  refresh={refresh}
                />
              ))}
          </section>
        );
      })}
    </>
  );
}
