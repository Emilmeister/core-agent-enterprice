import { useEffect, useRef, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { Peer } from "./types";

export function Agents({ api }: { api: Api }) {
  const [peers, setPeers] = useState<Peer[]>([]);
  const [selection, setSelection] = useState<Peer | "new" | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const read = useRef(0);
  const editor = useRef(0);
  const editorVersion = editor.current;
  function select(peer: Peer | "new" | null) {
    if (peer === selection) return;
    ++editor.current;
    setSelection(peer);
  }
  async function load(signal = request.current?.signal) {
    const version = ++read.current;
    const peers = await api.pages<Peer>("/api/remote-agents", "agents", signal);
    const current = !signal?.aborted && version === read.current;
    if (current) setPeers(peers);
    return current;
  }
  useEffect(() => {
    const abort = new AbortController();
    request.current = abort;
    void load(abort.signal)
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorText(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [api]);
  async function remove(peer: Peer) {
    if (!window.confirm(`Удалить подключение «${peer.name}»? Новые обращения к нему станут недоступны. Уже принятые задачи продолжат работу; история и файлы сохранятся.`)) return;
    setBusy(true);
    setError("");
    try {
      await api.json(`/api/remote-agents/${encodeURIComponent(peer.id)}/connection`, "DELETE",
        { expected_revision: peer.revision }, request.current?.signal);
      await load();
    } catch (failure) {
      if (!request.current?.signal.aborted) {
        const message = `Не удалось подтвердить удаление подключения. ${errorText(failure)}`;
        setError(message);
        await load().catch((refreshError) => {
          if (!request.current?.signal.aborted) setError(`${message} Не удалось обновить список: ${errorText(refreshError)}`);
        });
      }
    } finally {
      if (!request.current?.signal.aborted) setBusy(false);
    }
  }
  return (
    <section className="page">
      <div className="section-line">
        <div>
          <div className="eyebrow">Доверенные подключения</div>
          <h1>Внешние агенты</h1>
        </div>
        <button disabled={busy} onClick={() => select("new")}>
          Добавить агента <span aria-hidden="true">＋</span>
        </button>
      </div>
      <p className="lede">
        Адресаты для задач вашего агента. Секрет подключения хранится на
        сервере.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Загружаем подключения…</p>}
      {busy && <p role="status">Удаляем подключение…</p>}
      {selection && (
        <PeerEditor
          key={editorVersion}
          api={api}
          peer={selection === "new" ? null : selection}
          close={() => select(null)}
          saved={async () => {
            if (await load() && editor.current === editorVersion) {
              select(null);
              setError("");
            }
          }}
        />
      )}
      <div className="peer-list">
        {peers.map((peer) => (
          <article className="peer" key={peer.id}>
            <div
              className={`status-dot ${peer.enabled ? "green" : ""}`}
              aria-hidden="true"
            />
            <div>
              <div className="section-line">
                <h3>{peer.name}</h3>
                <span className="tag">
                  {peer.enabled ? "Включён" : "Выключен"}
                </span>
              </div>
              <p>{peer.description || "Без описания"}</p>
              <p className="endpoint">{peer.url}</p>
              <p className="muted">
                {peer.has_header_value ? "Секрет настроен" : "Без секрета"}
              </p>
            </div>
            <div className="peer-actions">
              <button className="secondary" disabled={busy} onClick={() => select(peer)}>
                Настроить
              </button>
              <button className="text-button danger" disabled={busy || !!selection} onClick={() => void remove(peer)}>
                Удалить
              </button>
            </div>
          </article>
        ))}
      </div>
      {!loading && !peers.length && !error && (
        <div className="empty">
          <h2>Пока нет подключений</h2>
          <p>Добавьте доверенного адресата, чтобы передавать ему задачи.</p>
        </div>
      )}
    </section>
  );
}
function PeerEditor({
  api,
  peer,
  close,
  saved,
}: {
  api: Api;
  peer: Peer | null;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [name, setName] = useState(peer?.name ?? "");
  const [url, setUrl] = useState(peer?.url ?? "");
  const [description, setDescription] = useState(peer?.description ?? "");
  const [enabled, setEnabled] = useState(peer?.enabled ?? true);
  const [header, setHeader] = useState(peer?.header_name ?? "Authorization");
  const [secret, setSecret] = useState("");
  const [secretMode, setSecretMode] = useState<"keep" | "replace" | "clear">(
    "keep",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const values = {
      url,
      description,
      enabled,
      header_name: header,
      ...(secretMode === "replace"
        ? { header_value: secret }
        : secretMode === "clear"
          ? { header_value: null }
          : {}),
    };
    try {
      await api.json(
        peer
          ? `/api/remote-agents/${encodeURIComponent(peer.id)}`
          : "/api/remote-agents",
        peer ? "PUT" : "POST",
        {
          ...values,
          ...(peer ? { expected_revision: peer.revision } : { name }),
        },
      );
      setSecret("");
      await saved();
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409) {
        setConflict(true);
        setSecret("");
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="form-sheet" onSubmit={save}>
      <div className="section-line">
        <h2>{peer ? `Настройка ${peer.name}` : "Новое подключение"}</h2>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setSecret("");
            close();
          }}
        >
          Закрыть
        </button>
      </div>
      <div className="form-grid">
        <label>
          Имя
          <input
            value={name}
            disabled={!!peer || busy}
            onChange={(e) => setName(e.target.value)}
            pattern={"[A-Za-z0-9_\\-]{1,128}"}
            maxLength={128}
            placeholder="weather-agent"
            title="От 1 до 128 символов: латинские буквы, цифры, _ и -."
            aria-describedby="peer-name-help"
            required
            autoComplete="off"
          />
          <span className="muted" id="peer-name-help">
            Латинские буквы, цифры, _ и -, до 128 символов. Например: weather-agent.
          </span>
        </label>
        <label>
          Адрес A2A
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={busy}
            placeholder="https://agent.example/a2a"
          />
        </label>
        <label className="full">
          Назначение
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          Имя заголовка
          <input
            value={header}
            onChange={(e) => setHeader(e.target.value)}
            disabled={busy}
            required
          />
        </label>
        <label>
          Секрет
          <select
            value={secretMode}
            onChange={(e) => {
              setSecretMode(e.target.value as typeof secretMode);
              setSecret("");
            }}
            disabled={busy}
          >
            <option value="keep">
              {peer?.has_header_value
                ? "Сохранить существующий"
                : "Без секрета"}
            </option>
            <option value="replace">Задать новое значение</option>
            <option value="clear">Очистить</option>
          </select>
        </label>
        {secretMode === "replace" && (
          <label className="full">
            Полное значение заголовка
            <input
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              disabled={busy}
              required
              autoComplete="new-password"
              placeholder="Bearer …"
            />
            <span className="muted">
              После сохранения значение не возвращается в интерфейс.
            </span>
          </label>
        )}
        <label className="check">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            disabled={busy}
          />
          Разрешить новые обращения
        </label>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button disabled={busy || conflict}>
          {busy ? "Сохраняем…" : "Сохранить подключение"}
        </button>
        {conflict && (
          <button
            type="button"
            className="secondary"
            onClick={() => void saved().catch((e) => setError(errorText(e)))}
          >
            Перечитать список
          </button>
        )}
      </div>
    </form>
  );
}
