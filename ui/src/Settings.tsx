import { useEffect, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { Settings as SettingsData, ToolPolicy } from "./types";
import { toolLabel } from "./toolPresentation";

const modes: Record<ToolPolicy["mode"], string> = {
  allow: "Без подтверждения",
  require_hitl: "С подтверждением",
  deny: "Запрещено",
};

const labels: Record<Exclude<keyof SettingsData, "revision">, string> = {
  hitl_timeout_seconds: "Ожидание разрешения, секунд",
  owner_answer_timeout_seconds: "Ожидание ответа владельца, секунд",
  guardrails_timeout_seconds: "Ожидание проверки материала, секунд",
  attachment_limit_bytes: "Общий размер вложений, байт",
  remote_timeout_seconds: "Ожидание внешнего агента, секунд",
  remote_poll_interval_seconds: "Интервал проверки после первых 13 минут, секунд",
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
      <p className="muted">Настройки общие для всех владельцев и всех чатов этого агента.</p>
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
                {key === "remote_poll_interval_seconds" && (
                  <small className="muted">
                    Первые 3 минуты — каждые 10 секунд, следующие 10 минут — каждые
                    30 секунд. Более короткий заданный интервал действует и на этих этапах.
                  </small>
                )}
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
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("all");
  const [modeFilter, setModeFilter] = useState("all");
  const [checksOff, setChecksOff] = useState(false);
  const query = search.trim().toLocaleLowerCase("ru-RU");
  const visible = tools.filter((tool) =>
    (!query || `${toolLabel(tool.canonical_name)} ${tool.canonical_name}`.toLocaleLowerCase("ru-RU").includes(query))
    && (source === "all" || (tool.origin.startsWith("builtin:") ? "builtin" : "mcp") === source)
    && (modeFilter === "all" || tool.mode === modeFilter)
    && (!checksOff || tool.guardrails_exempt),
  );
  const visibleNames = new Set(visible.map((tool) => tool.canonical_name));
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
        Определите, какие действия требуют подтверждения и проверки материалов.
      </p>
      <p className="muted">
        Правила общие для всех владельцев и всех чатов этого агента.
        Подтверждение действия и проверка его аргументов и результатов действуют отдельно.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Загружаем инструменты…</p>}
      <div className="tools-toolbar">
        <label>
          Поиск инструментов
          <input type="search" value={search}
            placeholder="Название или техническое имя"
            onChange={(event) => setSearch(event.target.value)} />
        </label>
        <label>
          Источник
          <select value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="all">Все источники</option>
            <option value="builtin">Встроенные</option>
            <option value="mcp">Подключённые MCP</option>
          </select>
        </label>
        <label>
          Режим выполнения
          <select value={modeFilter} onChange={(event) => setModeFilter(event.target.value)}>
            <option value="all">Все режимы</option>
            {Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={checksOff}
            onChange={(event) => setChecksOff(event.target.checked)} />
          Только с отключёнными проверками
        </label>
      </div>
      {!loading && tools.length > 0 && (
        <p className="tools-count muted" role="status">Показано {visible.length} из {tools.length}</p>
      )}
      <div className="policy-list">
        {tools.map((tool) => (
          <ToolEditor
            key={`${tool.canonical_name}:${tool.revision}:${tool.origin}`}
            api={api}
            tool={tool}
            hidden={!visibleNames.has(tool.canonical_name)}
            refresh={load}
          />
        ))}
      </div>
      {!loading && !tools.length && !error && (
        <p className="empty">
          В этом развёртывании нет доступных инструментов.
        </p>
      )}
      {!loading && tools.length > 0 && !visible.length && (
        <p className="empty">По этим условиям инструменты не найдены. Измените поиск или фильтры.</p>
      )}
    </section>
  );
}
function ToolEditor({
  api,
  tool,
  hidden,
  refresh,
}: {
  api: Api;
  tool: ToolPolicy;
  hidden: boolean;
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
    <form className="policy" data-tool-name={tool.canonical_name} hidden={hidden} onSubmit={save}>
      <div className="policy-heading">
        <h3>{toolLabel(tool.canonical_name)}</h3>
        <p className="policy-meta muted">
          <code>{tool.canonical_name}</code>
          <span> · </span>
          {tool.origin.startsWith("builtin:")
            ? "Встроенный инструмент"
            : "Подключённый MCP-инструмент"}
        </p>
      </div>
      <label className="policy-controls">
        Режим выполнения
        <select
          value={mode}
          disabled={busy}
          onChange={(e) => setMode(e.target.value as ToolPolicy["mode"])}
        >
          {Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label className="check policy-checks">
        <input
          type="checkbox"
          checked={!exempt}
          disabled={busy}
          onChange={(e) => setExempt(!e.target.checked)}
        />
        Проверять аргументы и результаты
      </label>
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
