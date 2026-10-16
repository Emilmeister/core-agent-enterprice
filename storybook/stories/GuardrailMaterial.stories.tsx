import type { Meta, StoryObj } from "@storybook/react-vite";
import { GuardrailMaterial } from "../../ui/src/GuardrailMaterial";
const meta = { title: "HITL/Содержимое материала", component: GuardrailMaterial } satisfies Meta<typeof GuardrailMaterial>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Structured: Story = { name: "Данные инструмента", args: { value: {
  review: { source_kind: "tool_result", review_id: "HIDDEN-REVIEW", revision: 4 },
  material: { payload: { tool_name: "inventory", tool_call_id: "HIDDEN-CALL", output: { order_id: "42", status: "ready", items: [{ name: "Заказ", count: 3 }] } } },
} }, play: async ({ canvasElement }) => {
  const text = canvasElement.textContent ?? "";
  if (!text.includes('"order_id": "42"') || !text.includes('"count": 3') || text.includes("HIDDEN-")) throw new Error("Business fields must survive without transport envelope");
} };
export const File: Story = { name: "Прикреплённый файл", args: { value: {
  review: { source_kind: "file_attachment" }, material: { payload: null, sealed_ref: { batch_id: "HIDDEN-BATCH", index: 0 }, file: { entry: { original_name: "Отчёт.docx" } } },
} } };
export const PythonError: Story = { name: "Ошибка выполнения Python", args: { value: {
  review: { source_kind: "tool_result", review_id: "HIDDEN-REVIEW" },
  material: { payload: { tool_name: "core_python_exec", tool_call_id: "HIDDEN-CALL", output: { error: { code: "COMMAND_NOT_FOUND", message: "Команда не найдена" } } } },
} }, play: async ({ canvasElement }) => {
  const text = canvasElement.textContent ?? "";
  if (!text.includes("Команда не найдена") || text.includes("не вернула текстового вывода") || text.includes("HIDDEN-")) throw new Error("Python error material must remain visible without review metadata");
} };
export const LongText: Story = { name: "Длинный текст целиком", args: { value: {
  review: { source_kind: "initial_input" }, material: { payload: "Первая строка\n" + "Строка материала\n".repeat(200) + "Последняя строка" },
} }, play: async ({ canvasElement }) => {
  const region = canvasElement.querySelector<HTMLElement>(".material-content")!;
  if (!region.textContent?.endsWith("Последняя строка") || region.scrollHeight <= region.clientHeight || region.tabIndex !== 0) throw new Error("Long material must remain complete and keyboard-scrollable");
} };
