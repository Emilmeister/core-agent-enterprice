import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { Api, ApiError, errorText } from "./api";

interface WorkspaceFile {
  name: string;
  path: string;
  size: number;
  mtime_ns: number;
  identity_token: string;
}
interface Preview {
  files: WorkspaceFile[];
  next_cursor: string | null;
  listed_at: number;
  active: boolean;
  workspace_revision: number;
  cleanup_block_reason: "CONTEXT_BUSY" | "WORKSPACE_CLEANUP_PENDING" | null;
}
interface Filters {
  directory: string;
  age: string;
}
interface CleanupBody {
  request_id: string;
  files: { path: string; identity_token: string }[];
}
interface CleanupReceipt {
  request_id: string;
  operation_id: string;
  files: CleanupBody["files"];
  state: "pending" | "completed" | "reconciliation";
  workspace_revision: number;
  results: {
    path: string;
    status: "pending" | "deleted" | "skipped" | "error";
    reason?:
      | "missing"
      | "identity_changed"
      | "unsafe_file"
      | "protected_file"
      | "filesystem_error"
      | "reconciliation_required";
    size?: number;
  }[];
  totals: {
    deleted: number;
    skipped: number;
    errors: number;
    deleted_bytes: number;
  };
}
interface CleanupOperation {
  body: CleanupBody;
  receipt: CleanupReceipt | null;
}
const cleanupReasons = {
  missing: "Файл уже отсутствует",
  identity_changed: "Файл изменился после просмотра",
  unsafe_file: "Небезопасный тип файла",
  protected_file: "Служебный файл защищён",
  filesystem_error: "Ошибка файловой системы",
  reconciliation_required: "Результат требует сверки на сервере",
};
const cleanupStatuses = {
  pending: "Ожидает",
  deleted: "Удалён",
  skipped: "Пропущен",
  error: "Ошибка",
};

export function selectWorkspaceFile(
  selected: WorkspaceFile[],
  file: WorkspaceFile,
  checked: boolean,
): WorkspaceFile[] {
  if (!checked) return selected.filter((item) => item.path !== file.path);
  if (
    selected.length >= 1000 ||
    selected.some((item) => item.path === file.path)
  )
    return selected;
  return [...selected, { ...file }];
}

export function cleanupBody(
  files: CleanupBody["files"],
  requestId: string,
): CleanupBody {
  return {
    request_id: requestId,
    files: files.map(({ path, identity_token }) => ({ path, identity_token })),
  };
}

export function cleanupReceipt(
  value: CleanupReceipt,
  body: CleanupBody,
): CleanupReceipt {
  const paths = new Set(body.files.map((file) => file.path));
  const integer = (number: number) =>
    Number.isSafeInteger(number) && number >= 0;
  if (
    !value ||
    typeof value.request_id !== "string" ||
    !value.request_id ||
    value.request_id !== body.request_id ||
    !Array.isArray(value.files) ||
    value.files.length > 1000 ||
    value.files.length !== body.files.length ||
    paths.size !== body.files.length ||
    value.files.some(
      (file, index) =>
        !file ||
        typeof file.path !== "string" ||
        !file.path ||
        file.path
          .split("/")
          .some((part) => !part || part === "." || part === "..") ||
        typeof file.identity_token !== "string" ||
        !file.identity_token ||
        file.path !== body.files[index].path ||
        file.identity_token !== body.files[index].identity_token,
    ) ||
    typeof value.operation_id !== "string" ||
    !value.operation_id ||
    !["pending", "completed", "reconciliation"].includes(value.state) ||
    !integer(value.workspace_revision) ||
    !Array.isArray(value.results) ||
    value.results.length !== paths.size ||
    new Set(value.results.map((item) => item?.path)).size !== paths.size ||
    value.results.some(
      (item) =>
        !item ||
        !paths.has(item.path) ||
        !Object.hasOwn(cleanupStatuses, item.status) ||
        (item.reason !== undefined &&
          !Object.hasOwn(cleanupReasons, item.reason)) ||
        (item.size !== undefined && !integer(item.size)) ||
        (value.state === "completed" && item.status === "pending"),
    ) ||
    !value.totals ||
    ![
      value.totals.deleted,
      value.totals.skipped,
      value.totals.errors,
      value.totals.deleted_bytes,
    ].every(integer) ||
    value.totals.deleted !==
      value.results.filter((item) => item.status === "deleted").length ||
    value.totals.skipped !==
      value.results.filter((item) => item.status === "skipped").length ||
    value.totals.errors !==
      value.results.filter((item) => item.status === "error").length
  ) {
    throw new Error("Invalid cleanup receipt");
  }
  return value;
}

const initialFilters: Filters = { directory: "", age: "" };

export function workspaceAge(value: string): string | null {
  if (value === "") return "";
  if (value.length > 64 || !/^[0-9]+(?:\.[0-9]+)?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const days = Number(whole);
  return Number.isFinite(days) &&
    days <= 100000 &&
    (days < 100000 || !/[1-9]/.test(fraction))
    ? value
    : null;
}

export function mergeWorkspaceFiles(
  current: WorkspaceFile[],
  page: WorkspaceFile[],
): WorkspaceFile[] {
  const files = new Map(current.map((file) => [file.path, file]));
  for (const file of page) files.set(file.path, file);
  return [...files.values()];
}

function previewResponse(value: Preview): Preview {
  if (
    !value ||
    !Array.isArray(value.files) ||
    value.files.length > 50 ||
    !(
      value.next_cursor === null ||
      (typeof value.next_cursor === "string" && value.next_cursor.length > 0)
    ) ||
    !Number.isFinite(value.listed_at) ||
    !Number.isFinite(new Date(value.listed_at * 1000).getTime()) ||
    typeof value.active !== "boolean" ||
    !Number.isSafeInteger(value.workspace_revision) ||
    value.workspace_revision < 0 ||
    ![null, "CONTEXT_BUSY", "WORKSPACE_CLEANUP_PENDING"].includes(
      value.cleanup_block_reason,
    ) ||
    value.files.some(
      (file) =>
        !file ||
        typeof file.name !== "string" ||
        !file.name ||
        typeof file.path !== "string" ||
        file.path
          .split("/")
          .some((part) => !part || part === "." || part === "..") ||
        !Number.isSafeInteger(file.size) ||
        file.size < 0 ||
        !Number.isInteger(file.mtime_ns) ||
        !Number.isFinite(new Date(file.mtime_ns / 1e6).getTime()) ||
        typeof file.identity_token !== "string" ||
        !file.identity_token,
    )
  ) {
    throw new Error("Invalid workspace preview");
  }
  return value;
}

function exactSize(files: WorkspaceFile[]): string {
  return (
    files
      .reduce((sum, file) => sum + BigInt(file.size), 0n)
      .toLocaleString("ru-RU") + " Б"
  );
}

function fileSize(bytes: number): string {
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
  const unit = Math.min(
    units.length - 1,
    Math.floor(Math.log2(Math.max(1, bytes)) / 10),
  );
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(bytes / 1024 ** unit)} ${units[unit]}`;
}

export function WorkspaceFiles({
  api,
  contextId,
  onClose,
  active,
  onDirty,
}: {
  api: Api;
  contextId: string;
  onClose: () => void;
  active: boolean;
  onDirty: (dirty: boolean) => void;
}) {
  const [selected, setSelected] = useState<WorkspaceFile[]>([]);
  const [confirmation, setConfirmation] = useState<WorkspaceFile[] | null>(
    null,
  );
  const [operation, setOperation] = useState<CleanupOperation | null>(null);
  const operationRef = useRef<CleanupOperation | null>(null);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const cleanupRequest = useRef<AbortController | null>(null);
  const [cleanupMessage, setCleanupMessage] = useState("");
  const deleteButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const lifetime = useRef(0);
  const localRevision = useRef(0);
  const latestRequest = useRef<AbortController | null>(null);
  const restoreDeleteFocus = useRef(false);
  const confirmationRef = useRef(confirmation);
  confirmationRef.current = confirmation;
  const unresolved = !!operation && operation.receipt?.state !== "completed";
  useLayoutEffect(() => {
    onDirty(unresolved || cleanupBusy);
    return () => onDirty(false);
  }, [unresolved, cleanupBusy, onDirty]);
  useLayoutEffect(() => {
    if (confirmation) cancelButton.current?.focus();
    else if (restoreDeleteFocus.current) {
      restoreDeleteFocus.current = false;
      deleteButton.current?.focus();
    }
  }, [confirmation]);
  const cancelConfirmation = () => {
    restoreDeleteFocus.current = true;
    setConfirmation(null);
  };
  const [directory, setDirectory] = useState("");
  const [age, setAge] = useState("");
  const [filters, setFilters] = useState(initialFilters);
  const [preview, setPreview] = useState<Preview | null>(null);
  const current = useRef<Preview | null>(null);
  const latestFilters = useRef(filters);
  latestFilters.current = filters;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const request = useRef<AbortController | null>(null);

  const loadLatestReceipt = useCallback(async () => {
    if (
      confirmationRef.current ||
      (operationRef.current &&
        operationRef.current.receipt?.state !== "completed")
    )
      return;
    latestRequest.current?.abort();
    const abort = new AbortController();
    latestRequest.current = abort;
    const revision = localRevision.current;
    const valid = () =>
      !abort.signal.aborted &&
      api.session.valid &&
      revision === localRevision.current &&
      !confirmationRef.current;
    try {
      const value = await api.json<CleanupReceipt>(
        `/api/chats/${encodeURIComponent(contextId)}/files/delete`,
        "GET",
        undefined,
        abort.signal,
      );
      if (!valid()) return;
      if (
        !value ||
        !Array.isArray(value.files) ||
        value.files.some((file) => !file)
      )
        throw new Error("Invalid cleanup receipt");
      const body = cleanupBody(value.files, value.request_id);
      const receipt = cleanupReceipt(value, body);
      const next = { body, receipt };
      operationRef.current = next;
      setOperation(next);
      setCleanupMessage("");
    } catch (failure) {
      if (
        !valid() ||
        (failure instanceof ApiError &&
          failure.code === "FILE_CLEANUP_NOT_FOUND")
      )
        return;
      setCleanupMessage(
        `Не удалось проверить предыдущую очистку. ${errorText(failure)}`,
      );
    }
  }, [api, contextId]);

  const load = useCallback(
    async (
      applied: Filters,
      cursor: string | null = null,
      refreshReceipt = true,
    ) => {
      const revision = ++generation.current;
      request.current?.abort();
      const abort = new AbortController();
      request.current = abort;
      const valid = () =>
        !abort.signal.aborted &&
        revision === generation.current &&
        api.session.valid;
      if (!cursor) {
        if (refreshReceipt) void loadLatestReceipt();
        if (
          !operationRef.current ||
          operationRef.current.receipt?.state === "completed"
        ) {
          setSelected([]);
          setConfirmation(null);
        }
        current.current = null;
        setPreview(null);
      }
      setLoading(true);
      setError("");
      try {
        const query = new URLSearchParams({ limit: "50" });
        if (applied.directory) query.set("directory", applied.directory);
        if (applied.age !== "") query.set("older_than_days", applied.age);
        if (cursor) query.set("cursor", cursor);
        const page = previewResponse(
          await api.json<Preview>(
            `/api/chats/${encodeURIComponent(contextId)}/files?${query}`,
            "GET",
            undefined,
            abort.signal,
          ),
        );
        if (!valid()) return;
        if (
          cursor &&
          (page.next_cursor === cursor ||
            (current.current &&
              (page.listed_at !== current.current.listed_at ||
                page.workspace_revision !==
                  current.current.workspace_revision)))
        ) {
          throw new Error("Invalid workspace cursor");
        }
        const next = {
          ...page,
          files: mergeWorkspaceFiles(
            cursor ? (current.current?.files ?? []) : [],
            page.files,
          ),
        };
        current.current = next;
        setPreview(next);
      } catch (failure) {
        if (!valid()) return;
        if (
          failure instanceof ApiError &&
          failure.code === "WORKSPACE_SCAN_LIMIT"
        ) {
          setError(
            "В этой папке слишком много файлов для одного просмотра. Укажите более узкий каталог и примените фильтры.",
          );
        } else if (
          failure instanceof ApiError &&
          failure.code === "WORKSPACE_UNAVAILABLE"
        ) {
          setError(
            "Не удалось прочитать папку: её содержимое могло измениться. Обновите просмотр.",
          );
        } else if (
          failure instanceof ApiError &&
          failure.status === 400 &&
          cursor
        ) {
          if (current.current) {
            current.current = { ...current.current, next_cursor: null };
            setPreview(current.current);
          }
          setError("Список устарел. Обновите просмотр, чтобы начать заново.");
        } else {
          setError(errorText(failure));
        }
      } finally {
        if (valid()) setLoading(false);
      }
    },
    [api, contextId, loadLatestReceipt],
  );

  const blocked =
    active || !!preview?.active || !!preview?.cleanup_block_reason;
  const cleanup = async (attempt: CleanupOperation, method: "GET" | "POST") => {
    if (cleanupRequest.current || !api.session.valid) return;
    const revision = lifetime.current;
    localRevision.current++;
    latestRequest.current?.abort();
    const abort = new AbortController();
    cleanupRequest.current = abort;
    operationRef.current = attempt;
    setOperation(attempt);
    setCleanupBusy(true);
    setCleanupMessage("");
    const valid = () =>
      !abort.signal.aborted &&
      revision === lifetime.current &&
      api.session.valid;
    try {
      const path = `/api/chats/${encodeURIComponent(contextId)}/files/delete`;
      const receipt = cleanupReceipt(
        await api.json<CleanupReceipt>(
          method === "GET"
            ? `${path}?${new URLSearchParams({ request_id: attempt.body.request_id })}`
            : path,
          method,
          method === "POST" ? attempt.body : undefined,
          abort.signal,
        ),
        attempt.body,
      );
      if (!valid()) return;
      const next = { ...attempt, receipt };
      operationRef.current = next;
      setOperation(next);
      if (receipt.state === "completed") {
        setSelected([]);
        void load(latestFilters.current, null, false);
      }
    } catch (failure) {
      if (!valid()) return;
      if (
        failure instanceof ApiError &&
        failure.code === "CONTEXT_BUSY" &&
        method === "POST"
      ) {
        operationRef.current = null;
        setOperation(null);
        setConfirmation(null);
        setCleanupMessage(
          "Задача работает. Удаление не началось. После её завершения выберите файлы и подтвердите удаление заново.",
        );
        void load(latestFilters.current, null, false);
      } else if (
        failure instanceof ApiError &&
        failure.code === "FILE_CLEANUP_NOT_FOUND" &&
        method === "GET"
      ) {
        setCleanupMessage(
          "Сервер пока не нашёл эту операцию. Можно явно повторить тот же запрос; новый идентификатор не создаётся.",
        );
      } else if (
        failure instanceof ApiError &&
        failure.code === "CLEANUP_REQUEST_CONFLICT"
      ) {
        setCleanupMessage(
          "Сервер отклонил конфликтующий запрос. Сохранены исходные данные операции; проверьте её результат.",
        );
      } else if (
        failure instanceof ApiError &&
        failure.code === "WORKSPACE_CLEANUP_INVALID"
      ) {
        setCleanupMessage(
          "Сервер не смог подтвердить состояние очистки. Новое удаление заблокировано; требуется проверка операции.",
        );
      } else {
        setCleanupMessage(
          `${errorText(failure)} Результат удаления не подтверждён. Проверьте его или явно повторите тот же запрос.`,
        );
      }
    } finally {
      if (valid()) {
        cleanupRequest.current = null;
        setCleanupBusy(false);
      }
    }
  };

  useLayoutEffect(() => {
    setDirectory("");
    setAge("");
    setFilters(initialFilters);
    void load(initialFilters);
    return () => {
      generation.current++;
      lifetime.current++;
      request.current?.abort();
      cleanupRequest.current?.abort();
      latestRequest.current?.abort();
    };
  }, [load]);

  return (
    <section
      className="workspace-files"
      id="workspace-files"
      aria-labelledby="workspace-files-title"
      aria-busy={loading}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          if (confirmation) cancelConfirmation();
          else onClose();
        }
      }}
    >
      <div className="section-line">
        <h2 id="workspace-files-title">Файлы чата</h2>
        <button className="text-button" onClick={onClose}>
          Закрыть
        </button>
      </div>
      <form
        className="workspace-file-filters"
        onSubmit={(event) => {
          event.preventDefault();
          const parsed = workspaceAge(age);
          if (parsed === null) {
            setError(
              "Укажите число дней от 0 до 100000 в десятичной записи, например 1.5.",
            );
            return;
          }
          const next = { directory, age: parsed };
          setFilters(next);
          void load(next);
        }}
      >
        <label htmlFor="workspace-directory">
          Папка
          <input
            id="workspace-directory"
            value={directory}
            onChange={(event) => setDirectory(event.target.value)}
            placeholder="Все папки"
            autoComplete="off"
          />
        </label>
        <label htmlFor="workspace-age">
          Старше, дней
          <input
            id="workspace-age"
            type="number"
            min="0"
            max="100000"
            step="any"
            value={age}
            onChange={(event) => setAge(event.target.value)}
            placeholder="Без фильтра"
          />
        </label>
        <button type="submit" className="secondary">
          Применить
        </button>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setDirectory("");
            setAge("");
            setFilters(initialFilters);
            void load(initialFilters);
          }}
        >
          Сбросить
        </button>
      </form>
      <div className="workspace-file-summary">
        <p className="muted">
          Папка: {filters.directory || "Все папки"}
          {filters.age !== "" && ` · Старше ${filters.age} дн.`}
        </p>
        <button className="text-button" onClick={() => void load(filters)}>
          Обновить просмотр
        </button>
      </div>
      {preview && (
        <p className="muted">
          Список на{" "}
          <time dateTime={new Date(preview.listed_at * 1000).toISOString()}>
            {new Date(preview.listed_at * 1000).toLocaleString("ru-RU")}
          </time>
          {(preview.active ||
            preview.cleanup_block_reason === "CONTEXT_BUSY") &&
            " · Задача работает; удаление недоступно."}
          {preview.cleanup_block_reason === "WORKSPACE_CLEANUP_PENDING" &&
            " · Предыдущая очистка ещё не завершена."}
        </p>
      )}
      {active && !preview?.active && (
        <p className="muted">Задача работает; удаление недоступно.</p>
      )}
      {cleanupMessage && (
        <p className="error" role="alert">
          {cleanupMessage}
        </p>
      )}
      {operation && (
        <section
          className="workspace-cleanup-result"
          aria-label="Результат удаления"
          aria-live="polite"
        >
          <h3>
            {operation.receipt?.state === "completed"
              ? "Удаление завершено"
              : operation.receipt?.state === "reconciliation"
                ? "Требуется сверка результата"
                : "Удаление: результат ещё не подтверждён"}
          </h3>
          <p className="muted workspace-file-path">
            Запрос: {operation.body.request_id}
          </p>
          {!operation.receipt && (
            <ul>
              {operation.body.files.map((file) => (
                <li className="workspace-file-path" key={file.path}>
                  {file.path}
                </li>
              ))}
            </ul>
          )}
          {operation.receipt && (
            <>
              <p>
                Удалено: {operation.receipt.totals.deleted} · Пропущено:{" "}
                {operation.receipt.totals.skipped} · Ошибок:{" "}
                {operation.receipt.totals.errors} · Освобождено:{" "}
                {fileSize(operation.receipt.totals.deleted_bytes)}
              </p>
              <ul>
                {operation.receipt.results.map((item) => (
                  <li key={item.path}>
                    <span className="workspace-file-path">{item.path}</span> —{" "}
                    {cleanupStatuses[item.status]}
                    {item.reason && `: ${cleanupReasons[item.reason]}`}
                  </li>
                ))}
              </ul>
            </>
          )}
          {unresolved && (
            <div className="workspace-cleanup-actions">
              <button
                className="secondary"
                disabled={cleanupBusy}
                onClick={() => void cleanup(operation, "GET")}
              >
                Проверить результат
              </button>
              <button
                className="text-button"
                disabled={cleanupBusy}
                onClick={() => void cleanup(operation, "POST")}
              >
                Повторить тот же запрос
              </button>
            </div>
          )}
          {cleanupBusy && <p role="status">Ожидаем ответ сервера…</p>}
        </section>
      )}
      {confirmation && (
        <section
          className="workspace-cleanup-confirm"
          role="region"
          aria-labelledby="workspace-confirm-title"
        >
          <h3 id="workspace-confirm-title">Удалить выбранные файлы?</h3>
          <p>
            Файлов: {confirmation.length} · Всего: {exactSize(confirmation)}.
            Это действие нельзя отменить.
          </p>
          <ul>
            {confirmation.map((file) => (
              <li key={file.path}>
                <strong>{file.name}</strong>
                <span className="workspace-file-path">{file.path}</span>
                <span>
                  {fileSize(file.size)} ({exactSize([file])})
                </span>
              </li>
            ))}
          </ul>
          <div className="workspace-cleanup-actions">
            <button
              ref={cancelButton}
              className="secondary"
              onClick={cancelConfirmation}
            >
              Отмена
            </button>
            <button
              className="danger"
              disabled={blocked || loading || cleanupBusy}
              onClick={() => {
                if (
                  !confirmation.length ||
                  blocked ||
                  loading ||
                  cleanupRequest.current
                )
                  return;
                const attempt = {
                  body: cleanupBody(confirmation, crypto.randomUUID()),
                  receipt: null,
                };
                setConfirmation(null);
                void cleanup(attempt, "POST");
              }}
            >
              Подтвердить удаление
            </button>
          </div>
        </section>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {loading && (
        <p className="muted" role="status">
          Загружаем файлы…
        </p>
      )}
      {preview && !preview.files.length && !loading && (
        <p className="workspace-file-empty">По этим условиям файлов нет.</p>
      )}
      {!!preview?.files.length && (
        <div className="workspace-file-table">
          <table>
            <caption className="sr-only">Файлы постоянной папки чата</caption>
            <thead>
              <tr>
                <th scope="col">Файл</th>
                <th scope="col">Размер</th>
                <th scope="col">Изменён</th>
              </tr>
            </thead>
            <tbody>
              {preview.files.map((file) => (
                <tr key={file.path}>
                  <td>
                    <label className="workspace-file-choice">
                      <input
                        type="checkbox"
                        checked={selected.some(
                          (item) => item.path === file.path,
                        )}
                        disabled={
                          unresolved ||
                          cleanupBusy ||
                          !!confirmation ||
                          (selected.length >= 1000 &&
                            !selected.some((item) => item.path === file.path))
                        }
                        onChange={(event) => {
                          const checked = event.currentTarget.checked;
                          setSelected((items) =>
                            selectWorkspaceFile(items, file, checked),
                          );
                        }}
                        aria-label={`Выбрать файл ${file.path}`}
                      />
                      <span className="workspace-file-name">{file.name}</span>
                    </label>
                    {file.path !== file.name && (
                      <span className="muted workspace-file-path">
                        {file.path}
                      </span>
                    )}
                  </td>
                  <td>{fileSize(file.size)}</td>
                  <td>
                    <time
                      dateTime={new Date(file.mtime_ns / 1e6).toISOString()}
                    >
                      {new Date(file.mtime_ns / 1e6).toLocaleString("ru-RU")}
                    </time>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!selected.length && !unresolved && !confirmation && (
        <div className="workspace-cleanup-actions">
          <p className="muted">
            Выбрано: {selected.length} / 1000 ·{" "}
            {fileSize(selected.reduce((sum, file) => sum + file.size, 0))}
          </p>
          <button
            ref={deleteButton}
            className="danger"
            disabled={blocked || loading || cleanupBusy}
            onClick={() => {
              localRevision.current++;
              latestRequest.current?.abort();
              setConfirmation(selected.map((file) => ({ ...file })));
            }}
          >
            Удалить выбранные…
          </button>
          <button className="text-button" onClick={() => setSelected([])}>
            Снять выбор
          </button>
        </div>
      )}
      {preview?.next_cursor && (
        <button
          className="secondary"
          disabled={loading}
          onClick={() => void load(filters, preview.next_cursor)}
        >
          Показать ещё
        </button>
      )}
    </section>
  );
}
