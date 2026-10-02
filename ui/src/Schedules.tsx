import { useCallback, useEffect, useRef, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { ChatRow, Schedule, Task } from "./types";

export function scheduleTime(value: string | null | undefined, timezone: string) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: timezone, dateStyle: "medium", timeStyle: "short",
    }).format(new Date(value));
  } catch { return "Дата недоступна"; }
}

type Pending = { path: string; method: string; body: Record<string, unknown>; kind: "create" | "update" | "delete" | "run" };
type Draft = { prompt: string; expression: string; timezone: string; context_id: string; enabled: boolean };
const blank: Draft = { prompt: "", expression: "0 9 * * *", timezone: "Europe/Moscow", context_id: "", enabled: true };

export function Schedules({ api, chats, onChat, refreshChats, onDirty }: {
  api: Api; chats: ChatRow[]; onChat: (row: ChatRow) => void;
  refreshChats: () => Promise<void>; onDirty: (value: boolean) => void;
}) {
  const [rows, setRows] = useState<Schedule[]>([]);
  const [selection, setSelection] = useState<Schedule | "new" | null>(null);
  const [draft, setDraft] = useState<Draft>(blank);
  const [changed, setChanged] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  const lifetime = useRef<AbortController | undefined>(undefined);
  const inFlight = useRef(false);
  const read = useRef(0);
  const load = useCallback(async (signal = lifetime.current?.signal) => {
    if (!signal || signal.aborted) return;
    const version = ++read.current;
    const next = await api.pages<Schedule>("/api/schedules", "schedules", signal);
    if (!signal.aborted && alive.current && version === read.current) { setRows(next); setLoading(false); }
  }, [api]);
  useEffect(() => {
    const abort = new AbortController();
    lifetime.current = abort;
    alive.current = true;
    let timer: ReturnType<typeof setTimeout>;
    async function tick() {
      try { await load(abort.signal); }
      catch (failure) { if (!abort.signal.aborted && alive.current) { setError(errorText(failure)); setLoading(false); } }
      if (!abort.signal.aborted && alive.current) timer = setTimeout(tick, 15000);
    }
    void tick();
    return () => { alive.current = false; abort.abort(); clearTimeout(timer); };
  }, [load]);
  useEffect(() => {
    const dirty = changed || !!pending || busy;
    onDirty(dirty);
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    if (dirty) window.addEventListener("beforeunload", warn);
    return () => { window.removeEventListener("beforeunload", warn); onDirty(false); };
  }, [changed, pending, busy, onDirty]);

  function discard() {
    if ((changed || pending) && !window.confirm(pending
      ? "Исход запроса неизвестен. Отказаться от повтора? Уже принятое действие не отменится."
      : "Отбросить изменения расписания?")) return false;
    setPending(null); setChanged(false); setSelection(null); setError("");
    return true;
  }
  async function edit(row: Schedule | "new") {
    const signal = lifetime.current?.signal;
    if (inFlight.current || !signal || signal.aborted || !discard()) return;
    setNotice("");
    if (row === "new") { setDraft({ ...blank }); setSelection("new"); return; }
    inFlight.current = true; setBusy(true);
    try {
      const { schedule } = await api.json<{ schedule: Schedule }>(`/api/schedules/${encodeURIComponent(row.id)}`, "GET", undefined, signal);
      if (!signal.aborted && alive.current) { setSelection(schedule); setDraft({ ...schedule }); }
    } catch (failure) { if (!signal.aborted && alive.current) setError(errorText(failure)); }
    finally { inFlight.current = false; if (alive.current) setBusy(false); }
  }
  async function execute(operation: Pending) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setPending(operation); setError(""); setNotice("");
    let accepted = false;
    try {
      const result = await api.json<{ task?: Task; schedule?: Schedule }>(operation.path, operation.method, operation.body);
      if (!alive.current) return;
      accepted = true; setPending(null); setChanged(false); setSelection(null);
      setNotice(operation.kind === "run"
        ? result.task?.metadata?.error ? "Запуск не принят: чат занят или задача недоступна. Откройте чат для проверки." : "Задача принята. Результат появится в чате."
        : operation.kind === "delete" ? "Расписание удалено. Чат, файлы и уже принятые задачи сохранены."
          : "Расписание сохранено.");
    } catch (failure) {
      if (!alive.current) return;
      const known = failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && failure.status !== 408;
      if (known) setPending(null);
      setError(known ? errorText(failure) : "Исход запроса не подтверждён. Повтор отправит то же тело и тот же идентификатор; новое действие пока заблокировано.");
      if (failure instanceof ApiError && failure.status === 409) {
        setSelection(null); setChanged(false);
        try { await load(); } catch { /* Keep the conflict message; refresh remains available. */ }
      }
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
    if (accepted) {
      try { await Promise.all([load(), refreshChats()]); }
      catch { if (alive.current) setError("Действие принято, но список не обновился. Обновите страницу расписаний."); }
    }
  }
  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!selection || pending) return;
    const creating = selection === "new";
    void execute({
      path: creating ? "/api/schedules" : `/api/schedules/${encodeURIComponent(selection.id)}`,
      method: creating ? "POST" : "PUT", kind: creating ? "create" : "update",
      body: creating ? { prompt: draft.prompt, expression: draft.expression, timezone: draft.timezone,
        ...(draft.context_id ? { context_id: draft.context_id } : {}), request_id: crypto.randomUUID() }
        : { prompt: draft.prompt, expression: draft.expression, timezone: draft.timezone,
          enabled: draft.enabled, expected_revision: selection.revision },
    });
  }
  const blocked = busy || !!pending;
  function openChat(row: Schedule) {
    onChat(chats.find((chat) => chat.context_id === row.context_id) ?? {
      context_id: row.context_id, latest_task_id: row.active_task_id, active: !!row.active_task_id,
    });
  }
  return <section className="page schedules">
    <div className="section-line"><div><div className="eyebrow">Регулярные задачи</div><h1>Расписания</h1></div>
      <button disabled={blocked} onClick={() => void edit("new")}>Создать расписание ＋</button></div>
    <p className="lede">Агент получает задачу в выбранном чате по расписанию. Пропущенный запуск отмечается в истории чата.</p>
    <button className="text-button" disabled={busy} onClick={() => {
      const signal = lifetime.current?.signal;
      void load(signal).catch((failure) => {
        if (alive.current && !signal?.aborted) setError(errorText(failure));
      });
    }}>Обновить список</button>
    {error && <p className="error" role="alert">{error}</p>}
    {notice && <p className="success" role="status">{notice}</p>}
    {pending && !busy && <div className="actions">
      <button onClick={() => void execute(pending)}>Повторить тот же запрос</button>
      <button className="secondary" onClick={discard}>Отказаться от повтора</button>
    </div>}
    {selection && <form className="form-sheet" onSubmit={save}>
      <div className="section-line"><h2>{selection === "new" ? "Новое расписание" : "Изменить расписание"}</h2>
        <button type="button" className="text-button" disabled={busy} onClick={discard}>Закрыть</button></div>
      <fieldset disabled={blocked} className="schedule-fields"><div className="form-grid">
        <label className="full">Задача<textarea required value={draft.prompt} onChange={(event) => { setDraft({ ...draft, prompt: event.target.value }); setChanged(true); }} /></label>
        <label>Расписание<input required maxLength={256} value={draft.expression} onChange={(event) => { setDraft({ ...draft, expression: event.target.value }); setChanged(true); }} />
          <span className="muted">Пять полей: минуты, часы, день месяца, месяц, день недели. Например: 0 9 * * mon-fri.</span></label>
        <label>Часовой пояс<input required value={draft.timezone} placeholder="Europe/Moscow" onChange={(event) => { setDraft({ ...draft, timezone: event.target.value }); setChanged(true); }} /></label>
        {selection === "new" ? <label className="full">Чат<select value={draft.context_id} onChange={(event) => { setDraft({ ...draft, context_id: event.target.value }); setChanged(true); }}>
          <option value="">Создать новый пустой чат</option>{chats.map((chat) => <option key={chat.context_id} value={chat.context_id}>{chat.title || "Новый чат"}</option>)}
        </select></label> : <label className="check full"><input type="checkbox" checked={draft.enabled} onChange={(event) => { setDraft({ ...draft, enabled: event.target.checked }); setChanged(true); }} />Расписание включено</label>}
      </div><div className="actions"><button type="submit">{busy ? "Сохраняем…" : "Сохранить"}</button></div></fieldset>
    </form>}
    {loading && <p role="status">Загружаем расписания…</p>}
    <div className="schedule-list">{rows.map((row) => <article className="schedule" key={row.id}>
      <div className="section-line"><h2 className="prose">{row.prompt}</h2><span className="tag">{row.enabled ? "Включено" : "Выключено"}</span></div>
      <p className="schedule-expression"><code>{row.expression}</code> · {row.timezone}</p>
      <p className="muted">Следующий запуск: {scheduleTime(row.next_due_at, row.timezone)}{row.active_task_id ? " · Чат занят" : ""}</p>
      <div className="actions">
        <button className="secondary" onClick={() => openChat(row)}>Открыть чат</button>
        <button className="secondary" disabled={blocked} onClick={() => void edit(row)}>Изменить</button>
        <button disabled={blocked || !!selection || !row.enabled || !!row.active_task_id} onClick={() => void execute({
          path: `/api/schedules/${encodeURIComponent(row.id)}/run-now`, method: "POST", kind: "run",
          body: { expected_revision: row.revision, request_id: crypto.randomUUID() },
        })}>Запустить сейчас</button>
        <button className="text-button danger" disabled={blocked || !!selection} onClick={() => {
          if (window.confirm("Удалить расписание? Чат, файлы и уже принятая задача сохранятся.")) void execute({
            path: `/api/schedules/${encodeURIComponent(row.id)}`, method: "DELETE", kind: "delete", body: { expected_revision: row.revision },
          });
        }}>Удалить</button>
      </div>
    </article>)}</div>
    {!loading && !rows.length && <div className="empty"><h2>Пока нет расписаний</h2><p>Выберите задачу, время и чат для её результатов.</p></div>}
  </section>;
}
