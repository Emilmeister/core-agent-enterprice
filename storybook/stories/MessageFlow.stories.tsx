import { useMemo, useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { Chat } from "../../ui/src/Chat";
import { ApiError } from "../../ui/src/api";
import type { ChatRow, HistoryItem, Message, Task } from "../../ui/src/types";
import { createDemoApi } from "./demoApi";
import { demoInteraction } from "./demoData";

function MessageFlow() {
  const [row, setRow] = useState<ChatRow | null>(null);
  const demo = useMemo(() => {
    const api = createDemoApi("empty");
    const fallback = api.request;
    const task: Task = { id: "flow-task", contextId: "flow-chat", status: { state: "TASK_STATE_WORKING" } };
    let items: HistoryItem[] = [];
    let resolve: ((value: Response) => void) | undefined;
    let waiting = false;
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
    const requests = new Map<string, string>();
    api.request = async (path, options) => {
      if (path.endsWith("message:send")) {
        const body = JSON.parse(String(options?.body)) as { message: Message };
        const old = requests.get(body.message.messageId);
        if (old && old !== String(options?.body)) throw new Error("Retry changed its body");
        requests.set(body.message.messageId, String(options?.body));
        if (!items.some(item => item.input_message_id === body.message.messageId)) items.unshift({
          id: body.message.messageId, input_message_id: body.message.messageId, task_id: task.id,
          kind: "placeholder", status: "pending_guardrail", text: "Hidden",
        });
        const response = await new Promise<Response>(done => { resolve = done; });
        if (!response.ok) throw new ApiError(response.status, "REQUEST_FAILED");
        return response;
      }
      if (path.includes("/history")) return json({ items, next_cursor: null });
      if (path.startsWith("/api/interactions?")) return json({ interactions: waiting ? [demoInteraction("owner_question")] : [], next_cursor: null });
      if (path.includes(":subscribe")) return new Response(new ReadableStream({ start(controller) {
        options?.signal?.addEventListener("abort", () => controller.close(), { once: true });
      } }), { headers: { "Content-Type": "text/event-stream" } });
      if (path.startsWith("/a2a/owner/tasks/")) return json(task);
      if (path.includes("/peer-conversations")) return json({ conversations: [], next_cursor: null });
      return fallback(path, options);
    };
    return { api, accept: () => { resolve?.(json({ task })); resolve = undefined; },
      lose: () => { resolve?.(json({ error: { code: "UNAVAILABLE" } }, 503)); resolve = undefined; },
      reject: () => { resolve?.(json({ error: { code: "REQUEST_INVALID" } }, 400)); resolve = undefined; },
      publish: () => { items = items.map(item => ({ ...item, kind: "user_message", status: "available",
        text: (JSON.parse(requests.get(item.input_message_id!)!) as {message: Message}).message.parts.map(part => part.text ?? part.filename).join("\n") })); },
      work: () => { items.unshift({ id: "call", task_id: task.id, kind: "tool_call", status: "available",
        text: JSON.stringify([{ id: "call", name: "core_terminal_exec", arguments: { argv: ["git", "status"] } }]) }); },
      wait: () => { waiting = true; task.status.state = "TASK_STATE_INPUT_REQUIRED"; },
      finish: () => { waiting = false; task.status.state = "TASK_STATE_COMPLETED";
        items.unshift({ id: "result", task_id: task.id, kind: "result", status: "available", text: "Готово!", outcome: { state: "COMPLETED" } }); },
    };
  }, []);
  return <div className="catalog">
    <p className="catalog-note">Отправьте сообщение. Кнопки управляют только демонстрацией; после изменения истории нажмите обновление в шапке чата.</p>
    <div className="actions">
      <button onClick={demo.accept}>Подтвердить приём</button><button onClick={demo.lose}>Потерять ответ</button>
      <button onClick={demo.reject}>Отклонить отправку</button><button onClick={demo.publish}>Опубликовать сообщение</button>
      <button onClick={demo.work}>Начать инструмент</button><button onClick={demo.wait}>Запросить ответ</button><button onClick={demo.finish}>Завершить</button>
    </div>
    <div className="catalog-screen"><Chat key={row?.context_id ?? "new"} api={demo.api} row={row}
      onTask={task => setRow({ context_id: task.contextId, latest_task_id: task.id, active: true })}
      onDirty={() => {}} onRenamed={() => {}} onDeleted={() => setRow(null)} /></div>
  </div>;
}
const meta = { title: "Экраны/Отправка сообщения", component: MessageFlow } satisfies Meta<typeof MessageFlow>;
export default meta;
export const Lifecycle: StoryObj<typeof meta> = { name: "Отправка, подтверждение и ответ", play: async ({ canvasElement }) => {
  const wait = (predicate: () => boolean) => new Promise<void>((resolve, reject) => {
    if (predicate()) { resolve(); return; }
    const observer = new MutationObserver(() => { if (predicate()) { observer.disconnect(); clearTimeout(timer); resolve(); } });
    const timer = setTimeout(() => { observer.disconnect(); reject(new Error("Message flow did not reach expected state")); }, 3000);
    observer.observe(canvasElement, { subtree: true, childList: true, attributes: true, characterData: true });
  });
  const click = (label: string) => [...canvasElement.querySelectorAll('button')].find(button => button.textContent === label)!.click();
  const input = canvasElement.querySelector('textarea')!;
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, 'Создай игру «Весёлая ферма»');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  await wait(() => !canvasElement.querySelector<HTMLButtonElement>('.composer-actions > button')!.disabled);
  canvasElement.querySelector<HTMLFormElement>('.composer')!.requestSubmit();
  await wait(() => !!canvasElement.querySelector('.from-owner') && !!canvasElement.querySelector('.typing-indicator'));
  if (input.value) throw new Error('Composer must clear immediately');
  click('Подтвердить приём');
  await wait(() => !!canvasElement.querySelector('[aria-label="Обновить историю"]'));
  if (canvasElement.querySelectorAll('.from-owner').length !== 1 || canvasElement.textContent?.includes('Обработка поручения')) throw new Error('New context must preserve one local echo');
} };
