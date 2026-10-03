import type { LiveAnswerSnapshot } from "./types";

const object = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;

export function chatStreamEvent(payload: unknown, taskId: string, contextId?: string):
  { kind: "canonical" } | { kind: "partial"; snapshot?: LiveAnswerSnapshot } | undefined {
  const envelope = object(payload);
  if (!envelope) return;
  const task = object(envelope.task);
  const update = object(envelope.statusUpdate);
  const artifact = object(envelope.artifactUpdate);
  if ([task, update, artifact].filter(Boolean).length !== 1) return;
  const event = task ?? update ?? artifact;
  if (!event || (task ? event.id : event.taskId) !== taskId
    || (contextId !== undefined && event.contextId !== contextId)) return;
  const status = object(update?.status);
  const message = object(status?.message);
  const metadata = object(message?.metadata);
  if (metadata?.partial !== true) return { kind: "canonical" };
  const ignored = { kind: "partial" } as const;
  const marker = object(metadata.core_agent_stream);
  if (!update || status?.state !== "TASK_STATE_WORKING" || message?.role !== "ROLE_AGENT"
    || message.taskId !== taskId || (contextId !== undefined && message.contextId !== contextId)
    || Object.hasOwn(metadata, "adk_thought") || Object.hasOwn(metadata, "adk_type")
    || marker?.version !== 1 || !positive(marker.generation) || !positive(marker.sequence)
    || (marker.superseded !== undefined && typeof marker.superseded !== "boolean")) return ignored;
  const superseded = marker.superseded === true;
  const parts = message.parts === undefined && superseded ? [] : message.parts;
  if (!Array.isArray(parts)) return ignored;
  const texts = parts.flatMap((value) => {
    const part = object(value);
    const meta = object(part?.metadata);
    return part && typeof part.text === "string" && part.data === undefined && part.raw === undefined && part.url === undefined
      && (part.mediaType === undefined || part.mediaType === "text/plain")
      && !(meta && (Object.hasOwn(meta, "adk_thought") || Object.hasOwn(meta, "adk_type"))) ? [part.text] : [];
  });
  const text = texts.join("\n");
  if ((!superseded && !texts.length) || (superseded && text)) return ignored;
  return { kind: "partial", snapshot: { taskId, generation: marker.generation, sequence: marker.sequence, text, superseded } };
}

export function nextLiveAnswer(current: LiveAnswerSnapshot | undefined, next: LiveAnswerSnapshot): LiveAnswerSnapshot {
  if (current?.taskId === next.taskId && (next.generation < current.generation
    || (next.generation === current.generation && (next.sequence <= current.sequence || current.superseded)))) return current;
  return next;
}
