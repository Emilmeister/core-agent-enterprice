import { useState } from "react";
import { Api, ApiError, errorText } from "./api";
import type { Interaction } from "./types";

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
    setBusy(true);
    setError("");
    try {
      await api.json(
        `/api/${routes[item.kind]}/${encodeURIComponent(item.wait_id)}/${item.kind === "owner_question" ? "answer" : "decision"}`,
        "POST",
        {
          subject_digest: item.subject_digest,
          ...(item.kind === "owner_question" ? { answer } : { decision }),
        },
      );
      setAnswer("");
      setMaterial(null);
      await refresh();
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409)
        await refresh().catch(() => {});
    } finally {
      setBusy(false);
    }
  }
  async function inspect() {
    setBusy(true);
    setError("");
    try {
      setMaterial(
        await api.json(
          `/api/guardrails/${encodeURIComponent(item.wait_id)}/material`,
        ),
      );
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }
  async function download() {
    setBusy(true);
    setError("");
    try {
      const response = await api.request(
        `/api/guardrails/${encodeURIComponent(item.wait_id)}/file`,
      );
      const blob = await response.blob();
      if (!api.session.valid) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "вложение";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className={`interaction ${closed ? "resolved" : ""}`}>
      <div className="eyebrow">
        {item.outcome
          ? "Решение сохранено"
          : expired
            ? "Срок ожидания истёк"
            : "Нужно ваше решение"}
      </div>
      <h3>{titles[item.kind] ?? "Ожидание"}</h3>
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
        До {new Date(item.deadline * 1000).toLocaleString("ru-RU")}
      </p>
      {item.kind !== "owner_question" && (
        <details>
          <summary>Посмотреть детали</summary>
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
          <button className="secondary" disabled={busy} onClick={inspect}>
            Просмотреть материал
          </button>
          {item.subject.affected_scope === "file_batch" && (
            <button className="secondary" disabled={busy} onClick={download}>
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
