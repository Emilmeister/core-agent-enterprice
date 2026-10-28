import { DetailsIcon } from "./DetailsIcon";
import { useEffect, useId, useRef, useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { Interaction } from "./types";
import { actionLabel, actionPreview } from "./toolPresentation";
import { GuardrailMaterial, guardrailReason } from "./GuardrailMaterial";
import type { MaterialSource } from "./GuardrailMaterial";

export function InteractionCard({
  api,
  item,
  refresh,
  source,
  historyId,
}: {
  api: Api;
  item: Interaction;
  refresh: () => Promise<void>;
  source?: MaterialSource;
  historyId?: string;
}) {
  const [answer, setAnswer] = useState("");
  const [selectedOption, setSelectedOption] = useState<number | "custom" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsId = useId();
  const answerGroupId = useId();
  const [material, setMaterial] = useState<Record<string, unknown> | null>(
    null,
  );
  const expired = item.deadline * 1000 <= Date.now();
  const closed = !!item.outcome || expired;
  const options = Array.isArray(item.subject.options)
    ? item.subject.options.filter((option): option is string => typeof option === "string") : [];
  const submittedAnswer = options.length > 0 && selectedOption !== "custom"
    ? typeof selectedOption === "number" ? options[selectedOption] ?? "" : "" : answer;
  const validAnswer = !!submittedAnswer.trim() && new TextEncoder().encode(submittedAnswer).length <= 65536;
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
    tool_approval: toolName === "core_ask_owner" ? "Разрешить задать уточняющий вопрос?" : actionLabel(toolName, args),
    owner_question: "Вопрос к вам",
    guardrail: "Проверка материала",
  };
  const routes = {
    tool_approval: "hitl",
    owner_question: "questions",
    guardrail: "guardrails",
  };
  async function decide(decision?: string) {
    if (closed || busy || (item.kind === "owner_question" && !validAnswer)) return;
    const controller = beginRequest();
    try {
      await api.json(
        `/api/${routes[item.kind]}/${encodeURIComponent(item.wait_id)}/${item.kind === "owner_question" ? "answer" : "decision"}`,
        "POST",
        {
          subject_digest: item.subject_digest,
          ...(item.kind === "owner_question" ? { answer: submittedAnswer } : { decision }),
        },
        controller.signal,
      );
      if (controller.signal.aborted || !api.session.valid) return;
      setAnswer("");
      setSelectedOption(null);
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
  if (item.outcome) {
    if (item.kind !== "owner_question") return null;
    const savedAnswer = typeof item.outcome.answer === "string" ? item.outcome.answer : null;
    const label = savedAnswer !== null ? "Ответ отправлен"
      : item.outcome.reason === "timeout" ? "Время ожидания ответа истекло"
      : item.outcome.reason === "cancelled" ? "Вопрос отменён" : "Вопрос закрыт";
    return <details id={`interaction-${item.wait_id}`} tabIndex={-1} data-kind={item.kind} className="owner-question-history">
      <summary>
        <svg className="disclosure-chevron" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m11 8 6 6-6 6" /></svg>
        {label}
      </summary>
      <div className="question-answer">
        {typeof item.subject.question === "string" && <div><strong>Вопрос агента</strong><p className="prose">{item.subject.question}</p></div>}
        {savedAnswer !== null && <div><strong>Ваш ответ</strong><p className="prose">{savedAnswer}</p></div>}
      </div>
    </details>;
  }
  return (
    <article id={`interaction-${item.wait_id}`} tabIndex={-1} data-kind={item.kind} data-history-id={historyId}
      className={`interaction ${closed ? "resolved" : ""}`}>
      <div className="interaction-status">
        {expired
            ? "Срок ожидания истёк"
            : item.kind === "owner_question" ? "Нужен ваш ответ" : "Нужно ваше разрешение"}
      </div>
      <h3>{titles[item.kind] ?? "Ожидание"}</h3>
      {item.kind === "tool_approval" && (
        <>
          {preview.length > 0 && (
            <dl className="action-preview">
              {preview.map((field, index) => (
                <div className="action-field" key={`${field.label}:${index}`}>
                  <dt className={field.label === "Вопрос" ? "sr-only" : undefined}>{field.label}</dt>
                  <dd className="prose">{field.value}</dd>
                </div>
              ))}
            </dl>
          )}
          <p className="muted interaction-explanation">{toolName === "core_ask_owner"
            ? "Разрешение позволит агенту задать этот вопрос. Сам ответ можно будет написать следующим шагом."
            : "Разрешение относится только к этому вызову. Результат появится после выполнения."}</p>
        </>
      )}
      {typeof item.subject.question === "string" && (
        <p className="prose">{item.subject.question}</p>
      )}
      {item.kind === "guardrail" && (
        <p>{guardrailReason(item.subject.reason)}</p>
      )}
      <p className="muted interaction-deadline" title={timezone}>
        Ответить до <time dateTime={new Date(item.deadline * 1000).toISOString()}>{new Date(item.deadline * 1000).toLocaleString("ru-RU", {
          day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short",
        })}</time>
      </p>
      {!closed &&
        (item.kind === "owner_question" ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void decide();
            }}
          >
            {options.length > 0 && <fieldset className="owner-answer-options" disabled={busy}>
              <legend className="sr-only">Варианты ответа</legend>
              {options.map((option, index) => <label className="owner-answer-option" key={index}>
                <input type="radio" name={answerGroupId} value={index} checked={selectedOption === index}
                  onChange={() => setSelectedOption(index)} />
                <span>{option}</span>
              </label>)}
              <label className="owner-answer-option">
                <input type="radio" name={answerGroupId} value="custom" checked={selectedOption === "custom"}
                  onChange={() => setSelectedOption("custom")} />
                <span>Свой ответ</span>
              </label>
            </fieldset>}
            {(options.length === 0 || selectedOption === "custom") && <label>
              Ваш ответ
              <textarea
                value={answer}
                onChange={(event) => setAnswer(event.target.value)}
                required
                disabled={busy}
              />
            </label>}
            <button disabled={busy || !validAnswer}>
              Отправить ответ
            </button>
          </form>
        ) : null)}
      {item.kind !== "owner_question" && <>
          <div className="actions interaction-actions">
            {!closed && <><button disabled={busy} onClick={() => void decide("allow")}>
              Разрешить
            </button>
            <button
              className="secondary danger"
              disabled={busy}
              onClick={() => void decide("reject")}
            >
              Отклонить
            </button></>}
            {item.kind === "guardrail" && <>
              <button className="secondary" disabled={busy || closed} onClick={inspect}>Просмотреть материал</button>
              {item.subject.affected_scope === "file_batch" && <button className="secondary" disabled={busy || closed} onClick={download}>Скачать проверяемый файл</button>}
            </>}
            {item.kind !== "guardrail" && <button type="button" className="details-toggle" aria-label="Технические данные" title="Технические данные"
              aria-expanded={detailsOpen} aria-controls={detailsId} onClick={() => setDetailsOpen(value => !value)}><DetailsIcon /></button>}
          </div>
          {item.kind !== "guardrail" && <div className="interaction-details" id={detailsId} hidden={!detailsOpen}><pre>{JSON.stringify(item.subject, null, 2)}</pre></div>}
      </>}
      {material && (
        <div className="material">
          <div className="section-line"><strong>Материал</strong>
            <button className="text-button" onClick={() => setMaterial(null)}>Скрыть</button>
          </div>
          <GuardrailMaterial value={material} source={source} />
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </article>
  );
}
