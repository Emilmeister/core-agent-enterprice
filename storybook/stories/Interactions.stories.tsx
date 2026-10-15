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
export const Guardrail: Story = { name: "Проверка материала", args: { kind: "guardrail" } };
