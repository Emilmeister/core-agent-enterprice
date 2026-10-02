import { useEffect, useRef, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { Interaction } from "./types";
import { actionLabel, actionPreview } from "./toolPresentation";

export function InteractionCard({
  api,
  item,
  refresh,
}: {
  api: Api;
  item: Interaction;
  refresh: () => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [material, setMaterial] = useState<Record<string, unknown> | null>(
    null,
  );
  const expired = item.deadline * 1000 <= Date.now();
  const closed = !!item.outcome || expired;
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    if (closed) {
      request.current?.abort();
      setMaterial(null);
    }
    return () => request.current?.abort();
  }, [api, item.context_id, item.wait_id, closed]);
  function beginRequest() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }
  const toolName = typeof item.subject.tool_name === "string"
    ? item.subject.tool_name : "Неизвестный инструмент";
  const rawArgs = item.subject.arguments;
  const resolved = item.subject.resolved_parameters;
  const args = toolName === "core_cron_create" && rawArgs && typeof rawArgs === "object"
    && resolved && typeof resolved === "object" && "timezone" in resolved
    && typeof resolved.timezone === "string"
    ? { ...rawArgs, timezone: resolved.timezone } : rawArgs;
  const preview = actionPreview(toolName, args);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const titles = {
    tool_approval: "Разрешение на действие",
    owner_question: "Вопрос к вам",
    guardrail: "Проверка материала",
  };
  const routes = {
    tool_approval: "hitl",
    owner_question: "questions",
    guardrail: "guardrails",
  };
  async function decide(decision?: string) {
    if (closed) return;
    const controller = beginRequest();
    try {
      await api.json(
        `/api/${routes[item.kind]}/${encodeURIComponent(item.wait_id)}/${item.kind === "owner_question" ? "answer" : "decision"}`,
        "POST",
        {
          subject_digest: item.subject_digest,
          ...(item.kind === "owner_question" ? { answer } : { decision }),
        },
        controller.signal,
      );
      if (controller.signal.aborted || !api.session.valid) return;
      setAnswer("");
      setMaterial(null);
      await refresh();
    } catch (failure) {
      if (controller.signal.aborted || !api.session.valid) return;
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409)
        await refresh().catch(() => {});
    } finally {
      if (!controller.signal.aborted && api.session.valid) setBusy(false);
    }
  }
  async function inspect() {
    if (closed) return;
    const controller = beginRequest();
    try {
      const data = await api.json<Record<string, unknown>>(
        `/api/guardrails/${encodeURIComponent(item.wait_id)}/material`,
        "GET", undefined, controller.signal,
      );
      if (!controller.signal.aborted && api.session.valid) setMaterial(data);
    } catch (failure) {
      if (!controller.signal.aborted && api.session.valid) setError(errorText(failure));
    } finally {
      if (!controller.signal.aborted && api.session.valid) setBusy(false);
    }
  }
  async function download() {
    if (closed) return;
    const controller = beginRequest();
    try {
      const response = await api.request(
        `/api/guardrails/${encodeURIComponent(item.wait_id)}/file`,
        { signal: controller.signal },
      );
      const blob = await response.blob();
      if (controller.signal.aborted || !api.session.valid) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "вложение";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      if (!controller.signal.aborted && api.session.valid) setError(errorText(failure));
    } finally {
      if (!controller.signal.aborted && api.session.valid) setBusy(false);
    }
  }
  if (item.outcome && item.kind !== "owner_question") return null;
  return (
    <article id={`interaction-${item.wait_id}`} tabIndex={-1}
      className={`interaction ${closed ? "resolved" : ""}`}>
      <div className="eyebrow">
        {item.outcome
          ? "Решение сохранено"
          : expired
            ? "Срок ожидания истёк"
            : "Нужно ваше решение"}
      </div>
      <h3>{titles[item.kind] ?? "Ожидание"}</h3>
      {item.kind === "tool_approval" && (
        <>
          <h4 className="interaction-heading">{actionLabel(toolName, args)}</h4>
          {preview.length > 0 && (
            <dl className="action-preview">
              {preview.map((field, index) => (
                <div className="action-field" key={`${field.label}:${index}`}>
                  <dt>{field.label}</dt>
                  <dd>{field.value}</dd>
                </div>
              ))}
            </dl>
          )}
          <p className="muted">Разрешение относится только к этому вызову. Результат появится после выполнения.</p>
        </>
      )}
      {typeof item.subject.question === "string" && (
        <p className="prose">{item.subject.question}</p>
      )}
      {item.kind === "guardrail" && (
        <p>
          Материал удерживается до решения. Разрешайте его, только если
          доверяете содержимому.
        </p>
      )}
      <p className="muted">
        Ответить до {new Date(item.deadline * 1000).toLocaleString("ru-RU", { timeZoneName: "short" })} · {timezone}
      </p>
      {item.kind !== "owner_question" && (
        <details>
          <summary>Технические данные</summary>
          <pre>{JSON.stringify(item.subject, null, 2)}</pre>
        </details>
      )}
      {item.outcome && (
        <p className="outcome">
          {item.outcome.reason === "allowed"
            ? "Разрешено"
            : item.outcome.reason === "rejected"
              ? "Отклонено"
              : item.outcome.reason === "timeout"
                ? "Срок истёк"
                : "Обработано"}
          {typeof item.outcome.answer === "string" &&
            ` · ${item.outcome.answer}`}
        </p>
      )}
      {item.kind === "guardrail" && (
        <div className="actions">
          <button className="secondary" disabled={busy || closed} onClick={inspect}>
            Просмотреть материал
          </button>
          {item.subject.affected_scope === "file_batch" && (
            <button className="secondary" disabled={busy || closed} onClick={download}>
              Скачать проверяемый файл
            </button>
          )}
        </div>
      )}
      {material && (
        <div className="material">
          <div className="section-line">
            <strong>Приватный материал</strong>
            <button className="text-button" onClick={() => setMaterial(null)}>
              Скрыть
            </button>
          </div>
          <pre>{JSON.stringify(material, null, 2)}</pre>
        </div>
      )}
      {!closed &&
        (item.kind === "owner_question" ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void decide();
            }}
          >
            <label>
              Ваш ответ
              <textarea
                value={answer}
                onChange={(event) => setAnswer(event.target.value)}
                required
                disabled={busy}
              />
            </label>
            <button
              disabled={
                busy ||
                !answer.trim() ||
                new TextEncoder().encode(answer).length > 65536
              }
            >
              Отправить ответ
            </button>
          </form>
        ) : (
          <div className="actions">
            <button disabled={busy} onClick={() => void decide("allow")}>
              Разрешить
            </button>
            <button
              className="secondary danger"
              disabled={busy}
              onClick={() => void decide("reject")}
            >
              Отклонить
            </button>
          </div>
        ))}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}
