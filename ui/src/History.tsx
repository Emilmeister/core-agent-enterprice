import { Markdown } from "./Markdown";
import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Api, errorText } from "./api";
import { scheduleTime } from "./Schedules";
import { InteractionCard } from "./Interactions";
import { byteCount, mergeHistoryPage } from "./types";
import type { AttachmentEntry, HistoryItem, HistoryPage, Interaction, ResponseFileEntry } from "./types";

const empty: HistoryPage = { items: [], next_cursor: null };
const statusLabels: Record<HistoryItem["status"], string> = {
  available: "",
  queued: "Сообщение принято и ожидает обработки",
  pending_guardrail: "Материал ожидает проверки",
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

export function Attachments({ entries }: { entries: AttachmentEntry[] }) {
  return <ul className="message-attachments" aria-label="Принятые вложения">
    {entries.map((entry) => <li key={entry.index}>
      <strong>{entry.actual_name}</strong>
      <span className="muted">{entry.relative_path}</span>
      <span className="muted">{byteCount(entry.size_bytes)}</span>
    </li>)}
  </ul>;
}

function ResponseFile({ api, contextId, taskId, file }: {
  api: Api; contextId: string; taskId: string; file: ResponseFileEntry;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | undefined>(undefined);
  const urls = useRef(new Set<string>());
  useLayoutEffect(() => {
    setBusy(false);
    setError("");
    pending.current = undefined;
    return () => {
      pending.current?.abort();
      for (const url of urls.current) URL.revokeObjectURL(url);
      urls.current.clear();
    };
  }, [api, contextId, taskId, file.file_id]);
  async function download() {
    if (pending.current || !api.session.valid) return;
    const abort = new AbortController();
    pending.current = abort;
    const stopped = () => abort.signal.aborted || pending.current !== abort || !api.session.valid;
    setBusy(true);
    setError("");
    try {
      const response = await api.request(
        `/api/chats/${encodeURIComponent(contextId)}/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(file.file_id)}`,
        { signal: abort.signal },
      );
      const blob = await response.blob();
      if (stopped()) return;
      const url = URL.createObjectURL(blob);
      urls.current.add(url);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.name.replace(/[\\/\u0000-\u001f\u007f]/g, "_") || "файл";
      link.click();
      window.setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url); }, 1000);
    } catch (failure) {
      if (!stopped()) setError(errorText(failure));
    } finally {
      if (!stopped()) setBusy(false);
      if (pending.current === abort) pending.current = undefined;
    }
  }
  return <li>
    <strong>{file.name}</strong>
    <span className="muted">{byteCount(file.size_bytes)}</span>
    <button type="button" className="secondary" disabled={busy || !api.session.valid}
      aria-label={`Скачать ${file.name}`} onClick={() => void download()}>
      {busy ? "Скачиваем…" : "Скачать"}
    </button>
    {error && <p className="error" role="alert">{error}</p>}
  </li>;
}

function Entry({ api, contextId, item }: { api: Api; contextId?: string; item: HistoryItem }) {
  if (item.kind === "schedule_notice") {
    const reasons: Record<string, string> = {
      context_busy: "чат был занят", late: "время запуска пропущено",
      service_unavailable: "сервис был недоступен",
      workspace_cleanup_pending: "ожидалось завершение очистки файлов",
    };
    const timezone = item.timezone ?? "Europe/Moscow";
    return <article className="schedule-notice">
      <p>Запуск по расписанию пропущен: {reasons[item.reason ?? ""] ?? "запуск был недоступен"}.</p>
      <p className="muted">{scheduleTime(item.due_at, timezone)}{item.through ? " — " + scheduleTime(item.through, timezone) : ""} · {timezone}</p>
    </article>;
  }
  const tool = item.kind === "tool_call" || item.kind === "tool_result";
  return (
    <article
      className={`message ${item.kind === "user_message" ? "from-owner" : ""} ${item.kind === "placeholder" ? "history-placeholder" : ""}`}
    >
      {tool ? (
        <details>
          <summary>
            {item.kind === "tool_call"
              ? "Вызов инструмента"
              : "Ответ инструмента"}
          </summary>
          <pre>{item.text}</pre>
        </details>
      ) : (
        <>
          <div className="eyebrow">
            {item.kind === "user_message"
              ? "Сообщение"
              : item.kind === "placeholder"
                ? "Статус сообщения"
                : "Core Agent"}
          </div>
          {item.kind === "placeholder" || item.status !== "available"
            ? item.status === "available" && (
                <p className="muted">Служебная запись агента</p>
              )
            : item.text && (item.kind === "user_message"
              ? <p className="prose">{item.text}</p>
              : <Markdown text={item.text} />)}
        </>
      )}
      {statusLabels[item.status] && (
        <p className="muted history-status">{statusLabels[item.status]}</p>
      )}
      {item.status === "available" && item.attachments?.length ? <Attachments entries={item.attachments} /> : null}
      {item.kind === "result" && item.status === "available" && item.outcome?.state === "COMPLETED"
        && contextId && item.task_id && item.response_files?.length ?
        <ul className="message-attachments response-files" aria-label="Файлы ответа">
          {item.response_files.map((file) => <ResponseFile key={`${contextId}:${item.task_id}:${file.file_id}`}
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
}: {
  api: Api;
  contextId?: string;
  history: ReturnType<typeof useChatHistory>;
  refresh: () => Promise<void>;
  onOlder: () => void;
}) {
  const groups: { root: string | null; items: HistoryItem[] }[] = [];
  for (const item of [...history.page.items].reverse()) {
    const previous = groups.at(-1);
    if (previous && previous.root === item.task_id) previous.items.push(item);
    else groups.push({ root: item.task_id, items: [item] });
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
      {(history.page.next_cursor || history.page.items.length > 0) && (
        <div className="history-pagination">
          {history.page.next_cursor && (
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
          )}
          <button
            className="text-button"
            disabled={history.loading}
            onClick={() => void history.load("reload")}
          >
            Обновить историю
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
          <button
            className="text-button"
            disabled={history.loading}
            onClick={() => void history.load("reload")}
          >
            Обновить историю
          </button>
        </div>
      )}
      {groups.map(({ root, items }, index) => {
        const interactions = root && !groups.slice(index + 1).some((group) => group.root === root)
          ? history.waits[root] ?? [] : [];
        return (
          <section key={items[0].id} className="history-run" aria-label="Обращение">
            {items.map((item) => {
              const review =
                item.review && !rendered.has(item.review.wait_id)
                  ? reviews.get(item.review.wait_id)
                  : undefined;
              if (review) rendered.add(review.wait_id);
              return (
                <Fragment key={item.id}>
                  <Entry api={api} contextId={contextId} item={item} />
                  {review && (
                    <InteractionCard
                      key={review.wait_id}
                      api={api}
                      item={review}
                      refresh={refresh}
                    />
                  )}
                </Fragment>
              );
            })}
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
