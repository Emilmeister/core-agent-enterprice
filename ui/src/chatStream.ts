const object = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
export function chatStreamEvent(payload: unknown, taskId: string, contextId?: string): boolean {
  const envelope = object(payload);
  if (!envelope) return false;
  const task = object(envelope.task);
  const update = object(envelope.statusUpdate);
  const artifact = object(envelope.artifactUpdate);
  if ([task, update, artifact].filter(Boolean).length !== 1) return false;
  const event = task ?? update ?? artifact;
  return !!event && (task ? event.id : event.taskId) === taskId
    && (contextId === undefined || event.contextId === contextId);
}
