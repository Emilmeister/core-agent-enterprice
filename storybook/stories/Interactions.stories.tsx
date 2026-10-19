import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, waitFor } from "storybook/test";
import { ApprovalExample } from "./Components.stories";
import { demoQuestion } from "./demoData";

const meta = { title: "HITL/Запросы владельцу", component: ApprovalExample,
  decorators: [(Story) => <div className="catalog"><Story /></div>],
  args: { kind: "tool_approval", resolved: false },
  argTypes: { kind: { control: "select", options: ["tool_approval", "owner_question", "guardrail"] } },
} satisfies Meta<typeof ApprovalExample>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Approval: Story = { name: "Разрешение задать вопрос" };
export const Question: Story = { name: "Ответ владельца", args: { kind: "owner_question" },
  play: async ({ canvasElement }) => {
    const form = canvasElement.querySelector<HTMLFormElement>(".interaction form")!;
    await userEvent.type(form.querySelector("textarea")!, "Для команды продаж, 5 слайдов.");
    await userEvent.click(form.querySelector("button")!);
    await waitFor(() => {
      const history = canvasElement.querySelector<HTMLDetailsElement>("details.owner-question-history");
      if (!history || history.open || history.querySelector("summary")?.textContent?.trim() !== "Ответ отправлен"
        || canvasElement.querySelector(".interaction form")) throw new Error("Saved answer must replace the active question with collapsed history");
    });
    await userEvent.click(canvasElement.querySelector(".catalog-reset")!);
  },
};
export const QuestionOptions: Story = {
  name: "Вопрос с вариантами ответа", args: { kind: "owner_question", options: ["Для команды продаж, 5 слайдов.", "Для руководства, 10 слайдов."] },
  play: async ({ canvasElement, args }) => {
    const form = () => canvasElement.querySelector<HTMLFormElement>(".interaction form")!;
    const choices = () => form().querySelectorAll<HTMLInputElement>('input[type="radio"]');
    if (choices().length !== 3 || [...choices()].some(choice => choice.checked)
      || !form().querySelector<HTMLButtonElement>("button")!.disabled || form().querySelector("textarea"))
      throw new Error("Options must start unselected and require an explicit answer submission");
    choices()[0].focus();
    await userEvent.keyboard(" ");
    if (!choices()[0].checked || canvasElement.querySelector(".owner-question-history"))
      throw new Error("Keyboard selection must choose an option without sending it");
    await userEvent.click(choices()[2]);
    const custom = "Для партнёров, 7 слайдов.";
    await userEvent.type(form().querySelector("textarea")!, custom);
    await userEvent.click(choices()[1]);
    if (form().querySelector("textarea")) throw new Error("Custom editor should be hidden while an option is selected");
    await userEvent.click(choices()[2]);
    if (form().querySelector("textarea")!.value !== custom) throw new Error("Switching choices must preserve the custom answer draft");
    await userEvent.click(choices()[0]);
    await userEvent.click(form().querySelector("button")!);
    await waitFor(() => {
      const answer = canvasElement.querySelector(".owner-question-history .question-answer > div:last-child p")?.textContent;
      if (answer !== args.options![0]) throw new Error("Submitted choice must match its exact original text");
    });
    await userEvent.click(canvasElement.querySelector(".catalog-reset")!);
    await userEvent.click(choices()[2]);
    await userEvent.type(form().querySelector("textarea")!, custom);
    await userEvent.click(form().querySelector("button")!);
    await waitFor(() => {
      const answer = canvasElement.querySelector(".owner-question-history .question-answer > div:last-child p")?.textContent;
      if (answer !== custom) throw new Error("Custom answers remain available even when suggestions exist");
    });
    await userEvent.click(canvasElement.querySelector(".catalog-reset")!);
  },
};
export const QuestionEmptyOptions: Story = { name: "Вопрос без предложенных вариантов", args: { kind: "owner_question", options: [] }, play: Question.play };
export const Answered: Story = { name: "Сохранённый ответ", args: { kind: "owner_question", resolved: true },
  play: async ({ canvasElement }) => {
    const history = canvasElement.querySelector<HTMLDetailsElement>("details.owner-question-history");
    if (!history || history.open || history.id !== "interaction-owner_question" || history.dataset.kind !== "owner_question"
      || history.querySelector("form, textarea, time") || /Ответить до|Решение сохранено/.test(history.textContent ?? ""))
      throw new Error("Resolved question must be collapsed history without pending controls or deadline");
    const summary = history.querySelector("summary")!;
    if (summary.textContent?.trim() !== "Ответ отправлен") throw new Error("Resolved answer requires an accurate summary");
    await userEvent.click(summary);
    if (!history.open || !history.querySelector(".question-answer")?.textContent?.includes(demoQuestion)
      || !history.querySelector(".question-answer")?.textContent?.includes("Для команды продаж, 5 слайдов."))
      throw new Error("Opening resolved history must preserve the complete question and answer");
    await userEvent.click(summary);
  },
};
export const TimedOut: Story = { name: "Ответ не получен вовремя", args: { kind: "owner_question", resolved: true, outcomeReason: "timeout" },
  play: async ({ canvasElement, args }) => {
    const history = canvasElement.querySelector<HTMLDetailsElement>("details.owner-question-history");
    const label = args.outcomeReason === "cancelled" ? "Вопрос отменён" : "Время ожидания ответа истекло";
    if (!history || history.open || history.querySelector("summary")?.textContent?.trim() !== label
      || history.querySelector("form, textarea, time") || history.textContent?.includes("Для команды продаж"))
      throw new Error("Unanswered question must preserve the actual outcome without an invented answer");
  },
};
export const Cancelled: Story = { name: "Вопрос отменён", args: { kind: "owner_question", resolved: true, outcomeReason: "cancelled" }, play: TimedOut.play };
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
