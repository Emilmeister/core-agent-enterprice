import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { History } from "../../ui/src/History";
import type { HistoryItem } from "../../ui/src/types";
import { createDemoApi } from "./demoApi";
import { demoInteraction } from "./demoData";

function ExecutionHistory({ approval = false, historicalWait = false }: { approval?: boolean; historicalWait?: boolean }) {
  const [api] = useState(createDemoApi);
  const [finished, setFinished] = useState(false);
  const items: HistoryItem[] = [];
  for (let i = 0; i < 7; i++) {
    const background = historicalWait && i === 0;
    items.push({ id: `call-${i}`, task_id: "demo-task", status: "available", kind: "tool_call",
      text: JSON.stringify([{ id: String(i), name: background ? "core_task_start" : "core_terminal_exec",
        arguments: background ? { tool: "core_terminal_exec" } : { argv: ["sh", "-c", "echo demo"] } }]) });
    if (i < 6 || finished) items.push({ id: `result-${i}`, task_id: "demo-task", status: "available", kind: "tool_result",
      text: JSON.stringify({ tool_call_id: String(i), status: i === 1 ? "failed" : "succeeded",
        output: background ? { task_id: "child", state: "working" } : { exit_code: i === 1 ? 2 : 0, stdout: "demo" } }) });
  }
  const wait = { ...demoInteraction(), source_id: "6", subject: { tool_name: "core_terminal_exec", arguments: { argv: ["sh", "-c", "echo demo"] } } };
  return <div className="catalog">
    <History api={api} contextId="demo" history={{ page: { items: items.reverse(), next_cursor: null },
      waits: approval && !finished ? { "demo-task": [wait] } : {}, loading: false, error: "", load: async () => {} }}
      refresh={async () => {}} onOlder={() => {}} />
    <button onClick={() => setFinished(true)}>Завершить последнее действие</button>
  </div>;
}
const meta = { title: "Действия/Ход выполнения", component: ExecutionHistory } satisfies Meta<typeof ExecutionHistory>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Mixed: Story = {
  name: "Успех, ошибка и выполнение",
  play: async ({ canvasElement }) => {
    const groups = canvasElement.querySelectorAll<HTMLDetailsElement>(".execution-group");
    if (groups.length !== 1 || groups[0].querySelectorAll(".action-card").length !== 7)
      throw new Error("All seven calls must share one execution group");
    const summary = groups[0].querySelector("summary")!.textContent!;
    if (!summary.includes("Ошибок: 1") || !summary.includes("Команда: sh"))
      throw new Error("Collapsed summary must expose error count and current action");
    groups[0].open = true;
  },
};
export const Approval: Story = {
  name: "Разрешение вне свёрнутого блока", args: { approval: true },
  play: async ({ canvasElement }) => {
    const approval = canvasElement.querySelector(".interaction");
    if (!approval || approval.closest(".execution-group"))
      throw new Error("Pending approval must remain outside execution group");
  },
};
export const BackgroundSnapshot: Story = {
  name: "Старый статус фоновой задачи не заменяет текущее действие",
  args: { historicalWait: true }, play: Mixed.play,
};
