import { useMemo, useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { Chat } from "../../ui/src/Chat";
import { demoRow } from "./demoData";
import { createDemoApi } from "./demoApi";

function RecoveryExample() {
  const [finished, setFinished] = useState(false);
  const api = useMemo(() => {
    const value = createDemoApi();
    const request = value.request;
    value.request = async (path, options) => {
      const response = await request(path, options);
      if (path.startsWith("/a2a/owner/tasks/")) {
        const task = await response.json();
        task.status = { state: finished ? "TASK_STATE_FAILED" : "TASK_STATE_WORKING" };
        task.metadata = finished ? {} : { core_agent_execution: { version: 1, state: "cleanup_pending" } };
        return new Response(JSON.stringify(task), { status: 200 });
      }
      return response;
    };
    return value;
  }, [finished]);
  return <div className="catalog">
    <p className="catalog-note">Прерванное выполнение. Данные демонстрационные, команды не запускаются.</p>
    <button onClick={() => setFinished(value => !value)}>{finished ? "Показать ожидание" : "Подтвердить остановку"}</button>
    <div className="catalog-screen"><Chat key={String(finished)} api={api} row={demoRow}
      onTask={() => {}} onDirty={() => {}} onRenamed={() => {}} onDeleted={() => {}} /></div>
  </div>;
}

const meta = { title: "Экраны/Восстановление выполнения", component: RecoveryExample } satisfies Meta<typeof RecoveryExample>;
export default meta;
export const CleanupPending: StoryObj<typeof meta> = {
  name: "Ожидание остановки и разблокировка удаления",
  play: async ({ canvasElement }) => {
    const wait = (predicate: () => boolean) => new Promise<void>((resolve, reject) => {
      if (predicate()) { resolve(); return; }
      const observer = new MutationObserver(() => {
        if (predicate()) { observer.disconnect(); clearTimeout(timer); resolve(); }
      });
      const timer = setTimeout(() => { observer.disconnect(); reject(new Error("Recovery state not displayed")); }, 3000);
      observer.observe(canvasElement, { subtree: true, childList: true, attributes: true, characterData: true });
    });
    await wait(() => !!canvasElement.textContent?.includes("Ожидает подтверждения остановки"));
    if (canvasElement.querySelector(".typing-indicator")) throw new Error("Cleanup must not show typing");
    const deletion = () => [...canvasElement.querySelectorAll("button")].find(button => button.textContent === "Удалить чат")!;
    if (!deletion().disabled) throw new Error("Active cleanup must block deletion");
    [...canvasElement.querySelectorAll("button")].find(button => button.textContent === "Подтвердить остановку")!.click();
    await wait(() => !!canvasElement.textContent?.includes("Не удалось завершить запрос"));
    if (deletion().disabled) throw new Error("Settled task must allow deletion");
  },
};
