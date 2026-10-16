import type { Meta, StoryObj } from "@storybook/react-vite";
import { ApprovalExample } from "./Components.stories";

const meta = { title: "HITL/Запросы владельцу", component: ApprovalExample,
  decorators: [(Story) => <div className="catalog"><Story /></div>],
  args: { kind: "tool_approval", resolved: false },
  argTypes: { kind: { control: "select", options: ["tool_approval", "owner_question", "guardrail"] } },
} satisfies Meta<typeof ApprovalExample>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Approval: Story = { name: "Разрешение задать вопрос" };
export const Question: Story = { name: "Ответ владельца", args: { kind: "owner_question" } };
export const Answered: Story = { name: "Сохранённый ответ", args: { kind: "owner_question", resolved: true } };
export const Guardrail: Story = { name: "Проверка материала", args: { kind: "guardrail" },
  play: async ({ canvasElement }) => {
    const inspect = [...canvasElement.querySelectorAll("button")].find(button => button.textContent === "Просмотреть материал")!;
    inspect.click();
    await new Promise<void>(resolve => {
      const observer = new MutationObserver(() => { if (canvasElement.querySelector(".material-content")) { observer.disconnect(); resolve(); } });
      observer.observe(canvasElement, { childList: true, subtree: true });
      setTimeout(() => { observer.disconnect(); resolve(); }, 2000);
    });
    const text = canvasElement.querySelector(".material-content")?.textContent ?? "";
    if (!text.includes("# README\n\n") || !text.includes("Конец материала.") || !text.includes("Предупреждение проверки"))
      throw new Error("Readable complete stdout and stderr are required");
    if (/PRIVATE-|completion_tokens|sandbox_terminated|revision/.test(canvasElement.textContent ?? "")
      || canvasElement.querySelector('[aria-label="Технические данные"], .material script'))
      throw new Error("Guardrail must not display technical metadata or execute HTML");
  },
};
