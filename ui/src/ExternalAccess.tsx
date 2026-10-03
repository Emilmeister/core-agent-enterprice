import { useEffect, useRef, useState } from "react";
import { Api, ApiError, errorText } from "./api";

type Account = {
  id: string;
  name: string;
  status: "pending" | "active" | "expired" | "revoked";
  created_at: number | null;
  issued_at: number | null;
  expires_at: number | null;
};
type Issued = { account: Account; access_token: string; expires_in: number };
const statuses = { pending: "Выдача не завершена", active: "Доступ выдан", expired: "Срок истёк", revoked: "Доступ отозван" };
function failureText(error: unknown) {
  if (error instanceof ApiError && error.code === "KEYCLOAK_ADMIN_ACCESS_DENIED")
    return "У вашей учётки недостаточно прав Keycloak для управления доступом. После назначения прав войдите снова.";
  if (error instanceof ApiError && error.code === "EXTERNAL_ACCESS_ALREADY_ISSUED")
    return "Доступ уже выдан. Прежний токен повторно не показывается. Найдите учётку в списке и выдайте новый токен.";
  return errorText(error);
}

export function ExternalAccess({ api }: { api: Api }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<Account | "new" | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const read = useRef(0);
  async function load(signal?: AbortSignal) {
    const version = ++read.current;
    const result = await api.json<{ accounts: Account[]; total: number }>("/api/external-access", "GET", undefined, signal);
    if (!signal?.aborted && version === read.current) setAccounts(result.accounts);
  }
  useEffect(() => {
    const abort = new AbortController();
    request.current = abort;
    void load(abort.signal).catch((error) => {
      if (!abort.signal.aborted) setError(failureText(error));
    }).finally(() => {
      if (!abort.signal.aborted) setLoading(false);
    });
    return () => abort.abort();
  }, [api]);
  async function changeAccess(account: Account, deleting = false) {
    const confirmation = deleting
      ? `Удалить учётку «${account.name}» навсегда? Её токены перестанут работать. Задачи и файлы останутся у владельцев; новая учётка не получит к ним доступ.`
      : `Отозвать доступ для «${account.name}»? Новые запросы будут отклоняться. Задачи и файлы сохранятся.`;
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    setError("");
    try {
      await api.json(`/api/external-access/${encodeURIComponent(account.id)}${deleting ? "/account" : ""}`, "DELETE", undefined, request.current?.signal);
      await load(request.current?.signal);
    } catch (error) {
      if (!request.current?.signal.aborted) {
        const message = deleting ? `Не удалось подтвердить удаление учётки. ${failureText(error)}` : failureText(error);
        setError(message);
        await load(request.current?.signal).catch((refreshError) => {
          if (!request.current?.signal.aborted) setError(`${message} Не удалось обновить список: ${failureText(refreshError)}`);
        });
      }
    } finally {
      if (!request.current?.signal.aborted) setBusy(false);
    }
  }
  return <section className="page">
    <div className="section-line">
      <div><h1>Доступ к агенту</h1><p className="muted" role="status">{loading ? "Загружаем учётки…" : `Внешних учёток: ${accounts.length}`}</p></div>
      <button disabled={busy} onClick={() => setSelection("new")}>Выдать доступ</button>
    </div>
    <p className="lede">Учётные записи внешних агентов, которые могут обращаться к этому агенту.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="access-list">
      {accounts.map((account) => <article className="access-row" key={account.id}>
        <div>
          <h3>{account.name}</h3>
          <p>{statuses[account.status]}</p>
          <p className="muted">{account.expires_at === null ? "Срок токена неизвестен" : `Токен действует до ${new Date(account.expires_at * 1000).toLocaleString()}`}</p>
        </div>
        <div className="access-actions">
          <button className="secondary" disabled={busy} onClick={() => setSelection(account)}>Выдать новый токен</button>
          <button className="text-button danger" disabled={busy || account.status === "revoked"} onClick={() => void changeAccess(account)}>Отозвать доступ</button>
          <button className="text-button danger" disabled={busy} onClick={() => void changeAccess(account, true)}>Удалить</button>
        </div>
      </article>)}
    </div>
    {!loading && !error && accounts.length === 0 && <div className="empty"><h2>Доступ пока никому не выдан</h2><p>Создайте учётку для первого внешнего агента.</p></div>}
    {selection !== null && <AccessDialog api={api} account={selection === "new" ? null : selection}
      close={() => setSelection(null)} changed={() => { void load(request.current?.signal).catch((error) => { if (!request.current?.signal.aborted) setError(failureText(error)); }); }} />}
  </section>;
}

function AccessDialog({ api, account, close, changed }: { api: Api; account: Account | null; close: () => void; changed: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const abort = useRef<AbortController | null>(null);
  const requestId = useRef(crypto.randomUUID());
  const [name, setName] = useState("");
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<Issued | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  useEffect(() => {
    abort.current = new AbortController();
    dialog.current?.showModal();
    return () => { abort.current?.abort(); };
  }, []);
  function finish() {
    setIssued(null);
    close();
  }
  async function issue(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api.json<Issued>(account ? `/api/external-access/${encodeURIComponent(account.id)}/token` : "/api/external-access",
        "POST", account ? { days } : { name, days, request_id: requestId.current }, abort.current?.signal);
      if (!abort.current?.signal.aborted) {
        setIssued(result);
        setUncertain(false);
        changed();
      }
    } catch (error) {
      if (!abort.current?.signal.aborted) {
        setError(failureText(error));
        setUncertain(!(error instanceof ApiError) || error.status >= 500);
        changed();
      }
    } finally {
      if (!abort.current?.signal.aborted) setBusy(false);
    }
  }
  async function copy() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.access_token);
      setCopied(true);
    } catch {
      setError("Не удалось скопировать автоматически. Выделите токен и скопируйте вручную.");
    }
  }
  return <dialog ref={dialog} className="access-dialog" aria-labelledby="access-dialog-title" onCancel={(event) => {
    if (busy) event.preventDefault();
    else finish();
  }}>
    <div className="section-line"><h2 id="access-dialog-title">{issued ? "Токен доступа" : account ? "Выдать новый токен" : "Выдать доступ"}</h2>
      <button className="text-button" disabled={busy} onClick={finish} aria-label="Закрыть окно выдачи доступа">Закрыть</button></div>
    {issued ? <div>
      <p>Доступ для «{issued.account.name}» выдан до {new Date((issued.account.expires_at ?? 0) * 1000).toLocaleString()}.</p>
      <p>Скопируйте токен сейчас. После закрытия окна он больше не будет доступен в интерфейсе.</p>
      <label>Адрес A2A<input readOnly value={`${location.origin}/a2a/external/`} /></label>
      <label>Токен доступа<textarea className="access-token" readOnly value={issued.access_token} spellCheck={false} autoComplete="off" /></label>
      <p className="muted">Передавайте его в заголовке Authorization: Bearer &lt;токен&gt;.</p>
      <div className="access-actions"><button onClick={() => void copy()}>{copied ? "Скопировано" : "Скопировать токен"}</button><button className="secondary" onClick={finish}>Готово</button></div>
      <span className="sr-only" role="status">{copied ? "Токен скопирован" : ""}</span>
    </div> : <form onSubmit={(event) => void issue(event)}>
      {account ? <p>Новый токен для «{account.name}» заменит прежний. Учётка, задачи и файлы сохранятся.</p>
        : <label>Название<input autoFocus value={name} onChange={(event) => setName(event.target.value)} maxLength={100} required disabled={busy || uncertain} /></label>}
      <label>Срок доступа, дней<input type="number" min={1} max={365} step={1} value={days} onChange={(event) => setDays(Number(event.target.value))} required disabled={busy || uncertain} /></label>
      {uncertain && <p role="status">Не удалось подтвердить выдачу. Учётка могла быть создана — список обновлён. {account ? "Закройте окно и явно выдайте новый токен." : "Можно повторить создание с тем же идентификатором; дубликат не создастся."}</p>}
      <div className="access-actions"><button disabled={busy || (!account && !name.trim()) || (account !== null && uncertain)}>{busy ? "Выдаём доступ…" : uncertain ? "Повторить тот же запрос" : account ? "Выдать новый токен" : "Выдать доступ"}</button><button type="button" className="secondary" disabled={busy} onClick={finish}>Отмена</button></div>
    </form>}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>;
}
