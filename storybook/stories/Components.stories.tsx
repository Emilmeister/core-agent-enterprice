import { useMemo, useRef, useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { InteractionCard } from "../../ui/src/Interactions";
import { ActionCard, ActionDialog } from "../../ui/src/ActionCard";
import type { HistoryAction } from "../../ui/src/ActionCard";
import { FileCard } from "../../ui/src/FileCard";
import { ArtifactCards } from "../../ui/src/ArtifactCards";
import { Markdown } from "../../ui/src/Markdown";
import { CodeBlock } from "../../ui/src/Code";
import { DetailsIcon } from "../../ui/src/DetailsIcon";
import type { Interaction } from "../../ui/src/types";
import { createDemoApi } from "./demoApi";
import { demoFile, demoInteraction, markdown } from "./demoData";

const meta = { title: "Обзор/Компоненты", excludeStories: ["ApprovalExample", "ActionExample"], tags: ["autodocs"], parameters: { layout: "padded" } } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

export function ApprovalExample(props: { kind?: Interaction["kind"]; resolved?: boolean }) {
  return <ApprovalState key={`${props.kind}:${props.resolved}`} {...props} />;
}
function ApprovalState({ kind = "tool_approval", resolved = false }: { kind?: Interaction["kind"]; resolved?: boolean }) {
  const [item, setItem] = useState(() => ({ ...demoInteraction(kind), outcome: resolved ? { reason: "answered", answer: "Для команды продаж, 5 слайдов." } : null } as Interaction));
  const decision = useRef<Record<string, unknown>>({});
  const api = useMemo(() => {
    const client = createDemoApi(); const request = client.request.bind(client);
    client.request = async (path, options) => {
      const response = await request(path, options);
      if (path.endsWith("/decision") || path.endsWith("/answer")) decision.current = (await response.clone().json()).outcome;
      return response;
    };
    return client;
  }, []);
  return <div>
    <InteractionCard api={api} item={item} refresh={async () => { setItem(previous => ({ ...previous, outcome: decision.current })); }} />
    {item.outcome && kind !== "owner_question" && <p className="catalog-note" role="status">Решение сохранено в демосценарии. Обработанная карточка скрыта, как в чате.</p>}
    <button className="text-button catalog-reset" onClick={() => setItem(demoInteraction(kind))}>Сбросить пример</button>
  </div>;
}

export function ActionExample({ state = "success" }: { state?: "success" | "error" | "active" | "unknown" }) {
  const [open, setOpen] = useState(false);
  const action: HistoryAction = { key: "demo-action", callId: "python", taskId: "demo-task", name: "core_python_exec",
    args: { code: 'total = sum([12000, 18000, 27500])\nprint(total)' },
    item: { id: "python-call", task_id: "demo-task", kind: "tool_call", status: "available", text: "" },
    ...(state === "active" ? {} : { result: state === "success" ? { status: "succeeded", output: { stdout: "57500\n", exit_code: 0 } }
      : state === "error" ? { status: "failed", error_code: "EXECUTION_FAILED", output: { stderr: "FileNotFoundError: report.csv", exit_code: 1 } }
      : { status: "unknown", error_code: "OUTCOME_UNKNOWN" } }) };
  return <><ActionCard action={action} onOpen={() => setOpen(true)} />{open && <ActionDialog action={action} onClose={() => setOpen(false)} />}</>;
}

export const Overview: Story = { name: "Все основные элементы", render: () => {
  const api = useMemo(() => createDemoApi(), []);
  return <main className="catalog">
    <h1>Каталог интерфейса</h1>
    <p className="catalog-note">Настоящие компоненты приложения на демонстрационных данных. Изменения не сохраняются на сервере. Слева доступны отдельные состояния и полноценные экраны.</p>
    <h2>Кнопки и раскрытия</h2>
    <div className="catalog-row"><button>Основное действие</button><button className="secondary">Второстепенное</button><button className="secondary danger">Отклонить</button><button disabled>Недоступно</button><button className="text-button">Отменить</button>
      <details><summary className="details-toggle" aria-label="Технические данные" title="Технические данные"><DetailsIcon /></summary><pre>Пример технических данных</pre></details></div>
    <h2>Поля ввода</h2><div className="catalog-grid"><label>Название<input placeholder="Введите название" /></label><label>Режим<select defaultValue="hitl"><option value="auto">Без подтверждения</option><option value="hitl">С подтверждением</option><option value="deny">Запрещено</option></select></label><label className="checkbox-label"><input type="checkbox" defaultChecked />Проверять аргументы и результаты</label></div>
    <h2>Сообщения о состоянии</h2><p className="success" role="status">Настройки сохранены.</p><p className="error" role="alert">Не удалось выполнить запрос. Повторите попытку.</p>
    <h2>Формирование ответа</h2><div className="typing-indicator" role="status" aria-label="Агент готовит ответ"><span /><span /><span /></div>
    <h2>Действие инструмента</h2><ActionExample />
    <h2>Запрос разрешения</h2><ApprovalExample />
    <h2>Файл</h2><ul className="message-attachments"><FileCard api={api} contextId="demo" taskId="demo-task" file={demoFile} /></ul>
    <h2>Markdown, код и диаграмма</h2><Markdown text={markdown} />
  </main>;
} };
export const MarkdownContent: Story = { name: "Markdown и Mermaid", render: () => <div className="catalog"><Markdown text={markdown} /></div> };
export const Python: Story = { name: "Подсветка Python", render: () => <div className="catalog"><CodeBlock language="python" text={'from decimal import Decimal\n\ndef total(values):\n    return sum(Decimal(value) for value in values)\n\nprint(total(["12.50", "7.50"]))'} /></div> };
export const Files: Story = { name: "Файлы и артефакты", render: () => {
  const api = useMemo(() => createDemoApi(), []);
  return <div className="catalog"><ArtifactCards api={api} contextId="demo" taskId="demo-task" artifacts={[
    { artifact_id: "files", name: "Файлы", parts: [{ ...demoFile, type: "file" }] },
    { artifact_id: "text", name: "Текстовый артефакт", parts: [{ ...demoFile, file_id: "text", type: "text" }] },
    { artifact_id: "data", name: "Структурированный артефакт", parts: [{ ...demoFile, file_id: "data", name: "report.json", media_type: "application/json", type: "data" }] },
  ]} /><h2>Недоступный файл</h2><ul className="message-attachments"><FileCard file={demoFile} available={false} /></ul></div>;
} };
