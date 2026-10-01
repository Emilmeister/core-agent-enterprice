import type { Session } from "./auth";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(
      status === 409
        ? "Данные уже изменились. Показано актуальное состояние; проверьте его перед повторным действием."
        : status === 403
          ? "Нет доступа к этому действию."
          : status === 404
            ? "Объект недоступен или больше не существует."
            : status === 413
              ? "Сообщение превышает допустимый размер."
              : status === 503
                ? "Сервис временно недоступен. Попробуйте позже."
                : "Не удалось выполнить запрос. Проверьте данные и повторите.",
    );
  }
}
export const errorText = (error: unknown) =>
  error instanceof ApiError
    ? error.message
    : error instanceof Error && error.message.startsWith("Сессия")
      ? error.message
      : "Связь прервана. Проверьте соединение и повторите действие.";

export class Api {
  constructor(readonly session: Session) {}
  async request(path: string, options: RequestInit = {}) {
    if (!path.startsWith("/api/") && !path.startsWith("/a2a/owner/"))
      throw new Error("Invalid API path");
    const token = await this.session.token();
    const headers = new Headers(options.headers);
    headers.set("Authorization", `Bearer ${token}`);
    headers.set("A2A-Version", "1.0");
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {
      ...options,
      headers,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    });
    if (!this.session.valid)
      throw new Error("Сессия завершена. Войдите снова.");
    if (response.status === 401) this.session.expire();
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new ApiError(
        response.status,
        typeof body?.error?.code === "string"
          ? body.error.code
          : "REQUEST_FAILED",
      );
    }
    return response;
  }
  async json<T>(
    path: string,
    method = "GET",
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    const response = await this.request(path, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    return response.json() as Promise<T>;
  }
  async pages<T>(
    path: string,
    field: string,
    signal?: AbortSignal,
  ): Promise<T[]> {
    const rows: T[] = [];
    const seen = new Set<string>();
    let cursor: string | null = null;
    do {
      const url = new URL(path, location.origin);
      url.searchParams.set("limit", "100");
      if (cursor) url.searchParams.set("cursor", cursor);
      const page = await this.json<Record<string, unknown>>(
        url.pathname + url.search,
        "GET",
        undefined,
        signal,
      );
      if (!Array.isArray(page[field])) throw new Error("Invalid page");
      rows.push(...(page[field] as T[]));
      cursor = typeof page.next_cursor === "string" ? page.next_cursor : null;
      if (cursor && seen.has(cursor)) throw new Error("Repeated page");
      if (cursor) seen.add(cursor);
    } while (cursor);
    return rows;
  }
  async subscribe(
    taskId: string,
    onEvent: () => Promise<void>,
    signal: AbortSignal,
  ) {
    const response = await this.request(
      `/a2a/owner/tasks/${encodeURIComponent(taskId)}:subscribe`,
      { method: "POST", headers: { Accept: "text/event-stream" }, signal },
    );
    if (
      !response.headers.get("content-type")?.includes("text/event-stream") ||
      !response.body
    )
      throw new Error("Invalid stream");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (!signal.aborted) {
        const chunk = await reader.read();
        if (chunk.done) return;
        buffer += decoder.decode(chunk.value, { stream: true });
        if (buffer.length > 1_048_576)
          throw new Error("Stream frame too large");
        buffer = buffer.replace(/\r\n/g, "\n");
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (frame.split("\n").some((line) => line.startsWith("data:")))
            await onEvent();
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
