import { useEffect, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { Settings as SettingsData, ToolPolicy } from "./types";

const labels: Record<Exclude<keyof SettingsData, "revision">, string> = {
  hitl_timeout_seconds: "Ожидание разрешения, секунд",
  owner_answer_timeout_seconds: "Ожидание ответа владельца, секунд",
  guardrails_timeout_seconds: "Ожидание проверки материала, секунд",
  attachment_limit_bytes: "Общий размер вложений, байт",
  remote_timeout_seconds: "Ожидание внешнего агента, секунд",
  remote_poll_interval_seconds: "Интервал проверки внешнего агента, секунд",
};
export function Settings({ api }: { api: Api }) {
  const [values, setValues] = useState<SettingsData | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .json<SettingsData>("/api/settings", "GET", undefined, controller.signal)
      .then(setValues)
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorText(e));
      });
    return () => controller.abort();
  }, [api]);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!values) return;
    setBusy(true);
    setError("");
    setNotice("");
    const { revision, ...settings } = values;
    try {
      setValues(
        await api.json("/api/settings", "PUT", {
          ...settings,
          expected_revision: revision,
        }),
      );
      setNotice("Настройки сохранены.");
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409)
        setValues(
          await api.json<SettingsData>("/api/settings").catch(() => values),
        );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="page">
      <div className="eyebrow">Рабочее пространство</div>
      <h1>Настройки</h1>
      <p className="lede">Сроки ожидания и ограничения для новых операций.</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="success" role="status">
          {notice}
        </p>
      )}
      {values ? (
        <form className="form-sheet" onSubmit={save}>
          <div className="form-grid">
            {Object.entries(labels).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  type="number"
                  min="1"
                  max="2147483647"
                  step="1"
                  required
                  disabled={busy}
                  value={
                    Number.isNaN(values[key as keyof SettingsData])
                      ? ""
                      : values[key as keyof SettingsData]
                  }
                  onChange={(event) =>
                    setValues({
                      ...values,
                      [key]:
                        event.target.value === ""
                          ? Number.NaN
                          : Number(event.target.value),
                    })
                  }
                />
              </label>
            ))}
          </div>
          <p className="muted">Срок уже созданного ожидания сохраняется.</p>
          <button
            disabled={
              busy ||
              Object.values(values).some((value) => !Number.isInteger(value))
            }
          >
            {busy ? "Сохраняем…" : "Сохранить настройки"}
          </button>
        </form>
      ) : (
        !error && <p role="status">Загружаем настройки…</p>
      )}
    </section>
  );
}

export function ToolPolicies({ api }: { api: Api }) {
  const [tools, setTools] = useState<ToolPolicy[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  async function load(notice = "") {
    const page = await api.json<{ tools: ToolPolicy[] }>("/api/tool-policies");
    setTools(page.tools);
    setError(notice);
  }
  useEffect(() => {
    let current = true;
    void api
      .json<{ tools: ToolPolicy[] }>("/api/tool-policies")
      .then((page) => {
        if (current) setTools(page.tools);
      })
      .catch((e) => {
        if (current) setError(errorText(e));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [api]);
  return (
    <section className="page">
      <div className="eyebrow">Контроль действий</div>
      <h1>Инструменты</h1>
      <p className="lede">
        Определите, какие действия требуют вашего разрешения.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Загружаем инструменты…</p>}
      <div className="policy-list">
        {tools.map((tool) => (
          <ToolEditor
            key={`${tool.canonical_name}:${tool.revision}:${tool.origin}`}
            api={api}
            tool={tool}
            refresh={load}
          />
        ))}
      </div>
      {!loading && !tools.length && !error && (
        <p className="empty">
          В этом развёртывании нет доступных инструментов.
        </p>
      )}
    </section>
  );
}
function ToolEditor({
  api,
  tool,
  refresh,
}: {
  api: Api;
  tool: ToolPolicy;
  refresh: (notice?: string) => Promise<void>;
}) {
  const [mode, setMode] = useState(tool.mode);
  const [exempt, setExempt] = useState(tool.guardrails_exempt);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.json(
        `/api/tool-policies/${encodeURIComponent(tool.canonical_name)}`,
        "PUT",
        {
          mode,
          guardrails_exempt: exempt,
          expected_revision: tool.revision,
          expected_origin: tool.origin,
        },
      );
      await refresh();
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409)
        await refresh(errorText(failure)).catch(() => {});
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="policy" onSubmit={save}>
      <div>
        <h3>{tool.canonical_name}</h3>
        <p className="muted">
          {tool.origin.startsWith("builtin:")
            ? "Встроенный инструмент"
            : "Подключённый MCP-инструмент"}
        </p>
      </div>
      <label>
        Доступ
        <select
          value={mode}
          disabled={busy}
          onChange={(e) => setMode(e.target.value as ToolPolicy["mode"])}
        >
          <option value="require_hitl">С разрешения владельца</option>
          <option value="allow">Разрешить</option>
          <option value="deny">Запретить</option>
        </select>
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={exempt}
          disabled={busy}
          onChange={(e) => setExempt(e.target.checked)}
        />
        Пропускать проверку материала
      </label>
      <p className="muted policy-note">
        Исключение относится к аргументам и результатам. Разрешение на само
        действие проверяется отдельно.
      </p>
      <div className="actions">
        <button
          disabled={
            busy || (mode === tool.mode && exempt === tool.guardrails_exempt)
          }
        >
          Сохранить
        </button>
        {error && (
          <button
            type="button"
            className="secondary"
            onClick={() => void refresh().catch((e) => setError(errorText(e)))}
          >
            Перечитать
          </button>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
