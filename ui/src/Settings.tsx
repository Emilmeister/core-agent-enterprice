import { useEffect, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { AgentSettings, McpConnection, Settings as SettingsData, ToolPolicy } from "./types";
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
    <section className="page settings-page">
      <div className="eyebrow">Рабочее пространство</div>
      <h1>Настройки</h1>
      <p className="lede">Сроки ожидания и ограничения для новых операций.</p>
      <p className="muted">Настройки общие для всех владельцев и всех чатов этого агента.</p>
      <AgentConfiguration api={api} />
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
        <form className="form-sheet runtime-settings" onSubmit={save}>
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

type McpDraft = McpConnection & {
  original_name?: string;
  header_value?: string | null;
  proxy_username?: string | null;
  proxy_password?: string | null;
  certificate_format?: "pkcs12" | "pem";
  certificate_data?: string;
  private_key_data?: string;
  certificate_password?: string;
  certificate_filename?: string;
  private_key_filename?: string;
  remove_certificate?: boolean;
};

function AgentConfiguration({ api }: { api: Api }) {
  const [current, setCurrent] = useState<AgentSettings | null>(null);
  const [profile, setProfile] = useState("");
  const [model, setModel] = useState<string | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [contextWindow, setContextWindow] = useState<string | null>(null);
  const [structuredOutput, setStructuredOutput] = useState<AgentSettings["structured_output"] | null>(null);
  const [guardrailsEnabled, setGuardrailsEnabled] = useState<boolean | null>(null);
  const [servers, setServers] = useState<McpDraft[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [modelError, setModelError] = useState("");
  const [notice, setNotice] = useState("");
  async function loadModels(signal?: AbortSignal) {
    setModelError("");
    try {
      const result = await api.json<{ models: string[] }>("/api/agent-settings/models", "GET", undefined, signal);
      if (!signal?.aborted) setModels(result.models);
    } catch (failure) {
      if (!signal?.aborted) setModelError(errorText(failure));
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    void api.json<AgentSettings>("/api/agent-settings", "GET", undefined, controller.signal).then((result) => {
      setCurrent(result);
      setProfile(result.profile_prompt);
      setModel(null);
      setContextWindow(null);
      setStructuredOutput(null);
      setGuardrailsEnabled(null);
      setServers(result.mcp_servers.map((server) => ({ ...server, original_name: server.name })));
    }).catch((failure) => {
      if (!controller.signal.aborted) setError(errorText(failure));
    });
    void loadModels(controller.signal);
    return () => controller.abort();
  }, [api]);
  async function save(section: string, values: Record<string, unknown>, mcpName?: string) {
    if (!current || busy) return;
    setBusy(section);
    setError("");
    setNotice("");
    try {
      const result = await api.json<AgentSettings>("/api/agent-settings", "PUT", {
        expected_revision: current.revision, ...values,
      });
      setCurrent(result);
      if (section === "profile") setProfile(result.profile_prompt);
      if (section === "model") {
        setModel(null);
        setContextWindow(null);
        setStructuredOutput(null);
      }
      if (section === "guardrails") setGuardrailsEnabled(null);
      if (section === "mcp") setServers((previous) => mcpName === undefined
        ? result.mcp_servers.map((server) => ({ ...server, original_name: server.name }))
        : previous.flatMap((server) => {
          if (server.name !== mcpName && server.original_name !== mcpName) return [server];
          const saved = result.mcp_servers.find((item) => item.name === mcpName);
          return saved ? [{ ...saved, original_name: saved.name }] : [];
        }));
      setNotice("Настройки агента сохранены. Они применятся к новым задачам.");
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409) {
        const result = await api.json<AgentSettings>("/api/agent-settings").catch(() => null);
        if (result) setCurrent(result);
      }
    } finally {
      setBusy("");
    }
  }
  function updateServer(index: number, values: Partial<McpDraft>) {
    setServers((previous) => previous.map((server, position) => position === index ? { ...server, ...values } : server));
  }
  async function readCertificate(index: number, file: File | undefined, privateKey = false) {
    if (!file || busy) return;
    if (file.size > 128 * 1024) { setError("Размер сертификата или ключа не должен превышать 128 КиБ."); return; }
    setBusy("mcp-file");
    setError("");
    try {
      const isPem = servers[index].certificate_format === "pem";
      const data = isPem ? await file.text() : btoa(Array.from(new Uint8Array(await file.arrayBuffer()), (byte) => String.fromCharCode(byte)).join(""));
      updateServer(index, privateKey ? { private_key_data: data, private_key_filename: file.name, remove_certificate: false }
        : { certificate_data: data, certificate_filename: file.name, remove_certificate: false });
    } catch {
      setError("Не удалось прочитать файл сертификата или ключа.");
    } finally { setBusy(""); }
  }
  function connectionPayload(server: McpDraft) {
    const identity = server.remove_certificate ? null : server.certificate_data
      ? server.certificate_format === "pem"
        ? { format: "pem", certificate: server.certificate_data, private_key: server.private_key_data, password: server.certificate_password }
        : { format: "pkcs12", data: server.certificate_data, password: server.certificate_password }
      : undefined;
    return { name: server.name, url: server.url, enabled: server.enabled, header_name: server.header_name,
      ...(server.proxy_url !== undefined ? { proxy_url: server.proxy_url } : {}),
      ...(server.proxy_username !== undefined || server.proxy_password !== undefined
        ? { proxy_username: server.proxy_username, proxy_password: server.proxy_password } : {}),
      ...(identity !== undefined ? { client_certificate: identity } : {}),
      ...(server.header_value !== undefined ? { header_value: server.header_value } : {}) };
  }
  async function saveConnection(index: number) {
    if (!current) return;
    const server = servers[index];
    const editingProxyAuth = server.proxy_username !== undefined || server.proxy_password !== undefined;
    const clearingProxyAuth = server.proxy_username === null && server.proxy_password === null;
    if (server.proxy_url && editingProxyAuth && !clearingProxyAuth
      && (!server.proxy_username || typeof server.proxy_password !== "string")) {
      setError("Для замены авторизации прокси укажите новый логин и пароль. Если пароль не нужен, отметьте «Использовать пустой пароль».");
      return;
    }
    if (!server.remove_certificate && ((server.private_key_data || server.certificate_password) && !server.certificate_data
      || server.certificate_data && server.certificate_format === "pem" && !server.private_key_data)) {
      setError("Выберите сертификат, а для PEM — также файл закрытого ключа.");
      return;
    }
    if (!server.original_name && current.mcp_servers.some((item) => item.name === server.name)) {
      setError("Подключение с таким именем уже существует. Выберите другое имя.");
      return;
    }
    if (server.original_name && !current.mcp_servers.some((item) => item.name === server.original_name)) {
      setError("Подключение уже удалено другим владельцем. Удалите его из формы и добавьте заново.");
      return;
    }
    const connections = current.mcp_servers.map((item) => connectionPayload(item.name === server.original_name ? server : item));
    if (!server.original_name) connections.push(connectionPayload(server));
    await save("mcp", { mcp_servers: connections }, server.name);
  }
  async function removeConnection(index: number) {
    if (!current) return;
    const server = servers[index];
    if (!server.original_name) {
      setServers((previous) => previous.filter((_, position) => position !== index));
      return;
    }
    if (!window.confirm(`Удалить MCP-подключение «${server.name}»?`)) return;
    await save("mcp", { mcp_servers: current.mcp_servers.filter((item) => item.name !== server.original_name).map(connectionPayload) }, server.original_name);
  }
  if (!current) return error ? <p className="error" role="alert">{error}</p> : <p role="status">Загружаем профиль агента…</p>;
  const selectedModel = model ?? current.model_id;
  const selectedContextWindow = contextWindow ?? (current.inherits.context_window ? "" : String(current.context_window));
  const structuredSettings = structuredOutput ?? current.structured_output;
  const modelDirty = (model !== null && model !== current.model_id) || contextWindow !== null || structuredOutput !== null;
  return <>
    {error && <p className="error" role="alert">{error}</p>}
    {notice && <p className="success" role="status">{notice}</p>}
    <form className="form-sheet agent-profile-settings" onSubmit={(event) => { event.preventDefault(); void save("profile", { profile_prompt: profile }); }}>
      <h2>Профиль агента</h2>
      <label>Системный промпт / профиль
        <textarea rows={8} disabled={!!busy} value={profile} onChange={(event) => setProfile(event.target.value)} />
      </label>
      <p className="muted">Роль, стиль и правила работы. Уже запущенные задачи сохраняют свой профиль.</p>
      <div className="actions">
        <button disabled={!!busy}>{busy === "profile" ? "Сохраняем…" : "Сохранить профиль"}</button>
        <button type="button" className="secondary" disabled={!!busy || current.inherits.profile_prompt}
          onClick={() => void save("profile", { profile_prompt: null })}>Использовать профиль развёртывания</button>
      </div>
    </form>
    <form className="form-sheet agent-model-settings" onSubmit={(event) => { event.preventDefault(); if (!modelDirty) return; void save("model", { ...(model !== null && model !== current.model_id ? { model_id: model } : {}),
      ...(contextWindow !== null ? { context_window: contextWindow === "" ? null : Number(contextWindow) } : {}),
      ...(structuredOutput ? { structured_output: structuredOutput } : {}) }); }}>
      <h2>Модель</h2>
      <label>Модель провайдера
        <select value={selectedModel} disabled={!!busy} onChange={(event) => { setModel(event.target.value); setContextWindow(""); }}>
          {[...new Set([current.model_id, selectedModel, ...models])].sort().map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </label>
      <label>Размер контекстного окна, токенов
        <input type="number" min={1024} max={100000000} step={1} value={selectedContextWindow}
          placeholder={String(current.context_window)} disabled={!!busy}
          onChange={(event) => setContextWindow(event.target.value)} />
      </label>
      <p className="muted">Сейчас: {current.context_window.toLocaleString("ru-RU")} токенов
        {current.inherits.context_window ? " из конфигурации развёртывания." : ", задано вручную."}
        {" "}Укажите размер из документации модели. Пустое поле возвращает значение развёртывания.</p>
      <label>Рассуждения для структурированных ответов
        <select value={structuredSettings.reasoning} disabled={!!busy} aria-describedby="structured-output-help"
          onChange={(event) => setStructuredOutput({ ...structuredSettings, reasoning: event.target.value as AgentSettings["structured_output"]["reasoning"] })}>
          <option value="auto">Автоматически</option>
          <option value="off">Выключено</option>
          <option value="inherit">Без переопределения</option>
        </select>
      </label>
      <p className="muted" id="structured-output-help">Для проверок материалов, сводок и извлечения сущностей памяти.
        {" "}«Автоматически» использует известные настройки провайдера и не оценивает качество ответов.
        {" "}Для неизвестного подключения настройки сохраняются. В режиме «Выключено» укажите
        {" "}поддерживаемый способ в дополнительных параметрах.
        {" "}Изменения применятся к новым задачам.</p>
      {structuredSettings.reasoning !== "inherit" && <details className="model-compatibility">
        <summary>Дополнительные параметры</summary>
        <label>Совместимость OpenAI API
          <select value={structuredSettings.compatibility} disabled={!!busy}
            onChange={(event) => setStructuredOutput({ ...structuredSettings, compatibility: event.target.value as AgentSettings["structured_output"]["compatibility"] })}>
            <option value="auto">Автоматически</option>
            <option value="chat_template">enable_thinking (vLLM)</option>
            <option value="thinking">thinking.type</option>
            <option value="reasoning_effort">reasoning_effort</option>
          </select>
        </label>
      </details>}
      {modelError && <p className="error" role="alert">{modelError} Текущая модель сохранена.</p>}
      <div className="actions">
        <button disabled={!!busy || !modelDirty || (selectedModel !== current.model_id && !models.includes(selectedModel))}>{busy === "model" ? "Сохраняем…" : "Сохранить модель"}</button>
        <button type="button" className="secondary" disabled={!!busy} onClick={() => void loadModels()}>Обновить список моделей</button>
        <button type="button" className="secondary" disabled={!!busy || (current.inherits.model_id && current.inherits.context_window && current.inherits.structured_output && model === null && contextWindow === null && !structuredOutput)}
          onClick={() => void save("model", { model_id: null, context_window: null, structured_output: null })}>Использовать модель развёртывания</button>
      </div>
    </form>
    <form className="form-sheet agent-guardrail-settings" onSubmit={(event) => {
      event.preventDefault();
      if (guardrailsEnabled !== null && guardrailsEnabled !== current.guardrails_enabled)
        void save("guardrails", { guardrails_enabled: guardrailsEnabled });
    }}>
      <h2>Проверка материалов</h2>
      <label className="check">
        <input type="checkbox" role="switch" checked={guardrailsEnabled ?? current.guardrails_enabled}
          disabled={!!busy} aria-describedby="guardrails-help"
          onChange={(event) => setGuardrailsEnabled(event.target.checked)} />
        Проверять материалы на prompt injection
      </label>
      <p className="muted" id="guardrails-help">Проверка входящих сообщений, файлов и результатов инструментов.
        {" "}Изменение применяется к новым задачам. Уже запущенные задачи и ожидающие решения сохраняют свои настройки.
        {" "}Разрешения на действия управляются отдельно.</p>
      <button disabled={!!busy || guardrailsEnabled === null || guardrailsEnabled === current.guardrails_enabled}>
        {busy === "guardrails" ? "Сохраняем…" : "Сохранить проверку материалов"}
      </button>
    </form>
    <section className="form-sheet agent-mcp-settings">
      <h2>MCP-серверы</h2>
      {servers.length === 0 && <p className="muted">Нет подключений.</p>}
      {servers.map((server, index) => <form className="agent-mcp-connection" key={index}
        onSubmit={(event) => { event.preventDefault(); void saveConnection(index); }}><fieldset disabled={!!busy}>
        <legend>Подключение {index + 1}</legend>
        <div className="form-grid">
          <label>Имя MCP-сервера
            <input value={server.name} pattern="[A-Za-z0-9_-]{1,64}" maxLength={64} required
              disabled={!!server.original_name}
              onChange={(event) => updateServer(index, { name: event.target.value })} />
          </label>
          <label>URL MCP-сервера
            <input type="url" value={server.url} maxLength={4096} required onChange={(event) => updateServer(index, { url: event.target.value })} />
          </label>
          <label>Имя заголовка авторизации
            <input value={server.header_name} maxLength={128} onChange={(event) => updateServer(index, { header_name: event.target.value })} />
          </label>
          <label>Значение заголовка авторизации
            <input type="password" autoComplete="new-password" value={server.header_value ?? ""}
              placeholder={server.has_header_value ? "Сохранённое значение" : "Не задано"}
              onChange={(event) => updateServer(index, { header_value: event.target.value || undefined })} />
          </label>
          <p className="muted">Значение передаётся как введено: например, Bearer …, Api-Key … или сам токен.</p>
          <label className="check"><input type="checkbox" checked={server.enabled}
            onChange={(event) => updateServer(index, { enabled: event.target.checked })} />Подключён</label>
          <label className="check"><input type="checkbox" checked={server.header_value === null}
            onChange={(event) => updateServer(index, { header_value: event.target.checked ? null : undefined })} />Удалить сохранённый ключ</label>
        </div>
        <details>
          <summary>Прокси и клиентский сертификат</summary>
          <div className="form-grid">
            <label>HTTP/HTTPS прокси
              <input type="url" maxLength={4096} value={server.proxy_url ?? ""} placeholder="http://proxy.example:8080"
                onChange={(event) => updateServer(index, { proxy_url: event.target.value || null,
                  ...(!event.target.value ? { proxy_username: undefined, proxy_password: undefined } : {}) })} />
            </label>
            <p className="muted">Пустое поле — прямое подключение. Настройка действует только для этого MCP.</p>
            <label>Логин прокси
              <input autoComplete="off" disabled={!server.proxy_url} value={server.proxy_username ?? ""}
                placeholder={server.has_proxy_credentials ? "Сохранён" : "Не задан"}
                onChange={(event) => updateServer(index, { proxy_username: event.target.value })} />
            </label>
            <label>Пароль прокси
              <input type="password" autoComplete="new-password" disabled={!server.proxy_url} value={server.proxy_password ?? ""}
                placeholder={server.proxy_username !== undefined ? "Укажите новый пароль" : server.has_proxy_credentials ? "Сохранён" : "Не задан"}
                onChange={(event) => updateServer(index, { proxy_password: event.target.value })} />
            </label>
            <label className="check"><input type="checkbox" checked={server.proxy_password === ""} disabled={!server.proxy_url}
              onChange={(event) => updateServer(index, { proxy_password: event.target.checked ? "" : undefined })} />Использовать пустой пароль</label>
            <label className="check"><input type="checkbox" checked={server.proxy_username === null} disabled={!server.proxy_url}
              onChange={(event) => updateServer(index, { proxy_username: event.target.checked ? null : undefined,
                proxy_password: event.target.checked ? null : undefined })} />Удалить логин и пароль прокси</label>
            <label>Формат клиентского сертификата
              <select value={server.certificate_format ?? "pkcs12"} onChange={(event) => updateServer(index, {
                certificate_format: event.target.value as "pkcs12" | "pem", certificate_data: undefined, private_key_data: undefined,
                certificate_password: undefined, certificate_filename: undefined, private_key_filename: undefined,
              })}>
                <option value="pkcs12">PKCS12 (.p12 / .pfx)</option><option value="pem">PEM: сертификат и ключ</option>
              </select>
            </label>
            <label>Файл клиентского сертификата
              <input key={`cert-${server.certificate_format}-${server.certificate_filename ?? ""}`} type="file"
                accept={server.certificate_format === "pem" ? ".pem,.crt" : ".p12,.pfx"}
                onChange={(event) => void readCertificate(index, event.target.files?.[0])} />
              {server.certificate_filename && <span>{server.certificate_filename}</span>}
            </label>
            {server.certificate_format === "pem" && <label>Файл закрытого ключа
              <input key={`key-${server.private_key_filename ?? ""}`} type="file" accept=".pem,.key"
                onChange={(event) => void readCertificate(index, event.target.files?.[0], true)} />
              {server.private_key_filename && <span>{server.private_key_filename}</span>}
            </label>}
            <label>Пароль файла сертификата / ключа
              <input type="password" autoComplete="new-password" value={server.certificate_password ?? ""}
                onChange={(event) => updateServer(index, { certificate_password: event.target.value || undefined })} />
            </label>
            <label className="check"><input type="checkbox" checked={!!server.remove_certificate}
              onChange={(event) => updateServer(index, { remove_certificate: event.target.checked,
                certificate_data: undefined, private_key_data: undefined, certificate_password: undefined,
                certificate_filename: undefined, private_key_filename: undefined })} />Удалить клиентский сертификат</label>
          </div>
          {server.has_client_certificate && <p className="muted">Клиентский сертификат сохранён.
            {server.certificate_info && <> {server.certificate_info.subject}. Действует до {server.certificate_info.expiry}.</>}</p>}
          <p className="muted">При изменении адреса MCP замените или удалите его сертификат. При смене прокси замените или удалите его логин и пароль.
            {" "}Доверенные CA общие для всех MCP и поставляются с образом; проверка TLS включена.</p>
        </details>
        <div className="actions">
          <button disabled={!!busy}>{busy === "mcp" ? "Сохраняем…" : "Сохранить подключение"}</button>
          <button type="button" className="secondary" onClick={() => void removeConnection(index)}>Удалить подключение</button>
        </div>
      </fieldset></form>)}
      <p className="muted">Ключи принимаются только при сохранении. При изменении адреса или заголовка замените ключ либо отметьте его удаление. Новые инструменты требуют подтверждения владельца.</p>
      <div className="actions">
        <button type="button" className="secondary" disabled={!!busy || servers.length >= 32}
          onClick={() => setServers((previous) => [...previous, { name: "", url: "", enabled: true, header_name: "Authorization", has_header_value: false }])}>Добавить MCP-сервер</button>
        <button type="button" className="secondary" disabled={!!busy || current.inherits.mcp_servers}
          onClick={() => void save("mcp", { mcp_servers: null })}>Использовать подключения развёртывания</button>
      </div>
    </section>
  </>;
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
        Подтверждение действия и проверка его результатов действуют отдельно.
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
