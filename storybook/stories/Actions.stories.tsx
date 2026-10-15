import type { Meta, StoryObj } from "@storybook/react-vite";
import { ActionExample } from "./Components.stories";
const meta = { title: "Действия/Вызов инструмента", component: ActionExample, args: { state: "success" },
  decorators: [(Story) => <div className="catalog"><Story /></div>],
  argTypes: { state: { control: "select", options: ["success", "error", "active", "unknown"] } },
} satisfies Meta<typeof ActionExample>;
export default meta;
type Story = StoryObj<typeof meta>;
export const ToolSuccess: Story = { name: "Инструмент — успех", args: { state: "success" } };
export const ToolError: Story = { name: "Инструмент — ошибка", args: { state: "error" } };
export const ToolActive: Story = { name: "Инструмент — выполнение", args: { state: "active" } };
export const ToolUnknown: Story = { name: "Инструмент — неизвестный результат", args: { state: "unknown" } };
