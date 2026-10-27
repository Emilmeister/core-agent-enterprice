import { useMemo, useState, type CSSProperties } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, waitFor } from "storybook/test";
import { Chat } from "../../ui/src/Chat";
import { Settings, ToolPolicies } from "../../ui/src/Settings";
import { Agents } from "../../ui/src/Agents";
import { Schedules } from "../../ui/src/Schedules";
import { ExternalAccess } from "../../ui/src/ExternalAccess";
import { WorkspaceFiles } from "../../ui/src/WorkspaceFiles";
import { PeerConversationCard, PeerConversationPanel } from "../../ui/src/PeerConversations";
import type { ChatRow } from "../../ui/src/types";
import type { DemoMode, DemoSettingsScenario } from "./demoData";
import { demoPeer, demoRow } from "./demoData";
import { createDemoApi } from "./demoApi";

const noop = () => {};
const done = async () => {};
const screens = ["Чат", "Инструменты", "Внешние агенты", "Доступ к агенту", "Настройки", "Расписания", "Файлы workspace", "Переписка агентов"] as const;
type Screen = typeof screens[number];
function ScreenExample({ screen, mode, working, settingsScenario }: { screen: Screen; mode: DemoMode; working: boolean; settingsScenario: DemoSettingsScenario }) {
  const [reset, setReset] = useState(0);
  return <div className="catalog">
    <p className="catalog-note">Демонстрационный экран. Настройки, удаления, токены и сообщения существуют только в памяти этой страницы.</p>
    <button className="secondary catalog-reset" onClick={() => setReset(value => value + 1)}>Сбросить демоданные</button>
    <ScreenContent key={`${screen}:${mode}:${working}:${settingsScenario}:${reset}`} screen={screen} mode={mode} working={working} settingsScenario={settingsScenario} />
  </div>;
}
function ScreenContent({ screen, mode, working, settingsScenario }: { screen: Screen; mode: DemoMode; working: boolean; settingsScenario: DemoSettingsScenario }) {
  const api = useMemo(() => createDemoApi(mode, working ? "TASK_STATE_WORKING" : undefined, settingsScenario), [mode, working, settingsScenario]);
  const [row, setRow] = useState<ChatRow | null>(mode === "empty" ? null : demoRow);
  const [peerOpen, setPeerOpen] = useState(false);
  const [peerWidth, setPeerWidth] = useState(480);
  if (screen === "Переписка агентов") return <><PeerConversationCard conversation={demoPeer} onOpen={() => setPeerOpen(true)} />
    {peerOpen && <div className="catalog-peer" style={{ "--peer-width": `${peerWidth}px` } as CSSProperties}><PeerConversationPanel api={api} contextId="demo" selected={demoPeer.peer_id} conversations={[demoPeer]}
      onSelect={noop} onClose={() => setPeerOpen(false)} width={peerWidth} maxWidth={860} onResize={setPeerWidth} /></div>}</>;
  return <div className="catalog-screen">
    {screen === "Чат" && <Chat api={api} row={row} onTask={task => setRow({ ...demoRow, latest_task_id: task.id })} onDirty={noop}
      onRenamed={value => setRow(current => current ? { ...current, ...value } : current)} onDeleted={() => setRow(null)} />}
    {screen === "Инструменты" && <ToolPolicies api={api} />}
    {screen === "Внешние агенты" && <Agents api={api} />}
    {screen === "Доступ к агенту" && <ExternalAccess api={api} />}
    {screen === "Настройки" && <Settings api={api} />}
    {screen === "Расписания" && <Schedules api={api} chats={[demoRow]} onChat={() => setRow(demoRow)} refreshChats={done} onDirty={noop} />}
    {screen === "Файлы workspace" && <WorkspaceFiles api={api} contextId="demo" active={false} onClose={noop} onDirty={noop} />}
  </div>;
}
const meta = { title: "Экраны/Приложение", component: ScreenExample,
  args: { screen: "Чат", mode: "normal", working: false, settingsScenario: "default" },
  argTypes: { screen: { control: "select", options: screens }, mode: { control: "select", options: ["normal", "empty", "loading", "error"] }, working: { control: "boolean" }, settingsScenario: { control: "select", options: ["default", "off", "inherit", "discovery-error", "conflict", "guardrails-off", "next-steps-on", "next-steps-unavailable"] } },
} satisfies Meta<typeof ScreenExample>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Conversation: Story = { name: "Чат" };
export const Preparing: Story = { name: "Чат — подготовка ответа", args: { working: true } };
export const Empty: Story = { name: "Пустой чат", args: { mode: "empty" } };
export const Loading: Story = { name: "Загрузка", args: { mode: "loading", screen: "Внешние агенты" } };
export const Error: Story = { name: "Ошибка сервиса", args: { mode: "error", screen: "Внешние агенты" } };
export const Tools: Story = { name: "Инструменты", args: { screen: "Инструменты" } };
export const Connections: Story = { name: "Внешние агенты", args: { screen: "Внешние агенты" } };
export const Access: Story = { name: "Доступ к агенту", args: { screen: "Доступ к агенту" } };
export const Configuration: Story = { name: "Настройки", args: { screen: "Настройки" } };
export const GuardrailsOff: Story = { name: "Настройки — проверка материалов выключена", args: { screen: "Настройки", settingsScenario: "guardrails-off" } };
export const NextStepsOn: Story = { name: "Настройки — планирование включено", args: { screen: "Настройки", settingsScenario: "next-steps-on" } };
export const NextStepsUnavailable: Story = {
  name: "Настройки — планирование недоступно", args: { screen: "Настройки", settingsScenario: "next-steps-unavailable" },
  play: async ({ canvas }) => {
    const toggle = await canvas.findByRole("switch", { name: "Планировать следующие шаги" }) as HTMLInputElement;
    const period = canvas.getByLabelText("Период, завершённых вызовов инструментов") as HTMLInputElement;
    if (!toggle.disabled || toggle.checked || !period.disabled)
      throw new globalThis.Error("Unavailable planning controls must remain disabled");
    canvas.getByText("Планирование недоступно: инструмент запрещён конфигурацией развёртывания.");
  },
};
export const NextStepsConfiguration: Story = {
  name: "Настройки — сохранение и сброс планирования", args: { screen: "Настройки" },
  play: async ({ canvas }) => {
    const toggle = await canvas.findByRole("switch", { name: "Планировать следующие шаги" }) as HTMLInputElement;
    const period = canvas.getByLabelText("Период, завершённых вызовов инструментов") as HTMLInputElement;
    const save = canvas.getByRole("button", { name: "Сохранить планирование" }) as HTMLButtonElement;
    const reset = canvas.getByRole("button", { name: "Использовать планирование развёртывания" }) as HTMLButtonElement;
    if (toggle.checked || period.value !== "5" || !save.disabled || !reset.disabled)
      throw new globalThis.Error("Planning must initially inherit the disabled deployment default");
    await userEvent.click(toggle);
    await userEvent.clear(period);
    if (!save.disabled) throw new globalThis.Error("An empty planning period cannot be saved");
    await userEvent.type(period, "0");
    if (!save.disabled) throw new globalThis.Error("A nonpositive planning period cannot be saved");
    await userEvent.clear(period);
    await userEvent.type(period, "1.5");
    if (!save.disabled) throw new globalThis.Error("A fractional planning period cannot be saved");
    await userEvent.clear(period);
    await userEvent.type(period, "3");
    await userEvent.click(save);
    await waitFor(() => {
      canvas.getByText("Настройки планирования заданы вручную.");
      if (!toggle.checked || period.value !== "3" || !save.disabled || reset.disabled)
        throw new globalThis.Error("Saved planning settings must clear the draft and allow reset");
    });
    await userEvent.click(reset);
    await waitFor(() => {
      if (toggle.checked || period.value !== "5" || !reset.disabled)
        throw new globalThis.Error("Reset must restore both deployment planning defaults");
    });
  },
};
export const NextStepsConflict: Story = {
  name: "Настройки — конфликт планирования", args: { screen: "Настройки", settingsScenario: "conflict" },
  play: async ({ canvas }) => {
    const toggle = await canvas.findByRole("switch", { name: "Планировать следующие шаги" }) as HTMLInputElement;
    const period = canvas.getByLabelText("Период, завершённых вызовов инструментов") as HTMLInputElement;
    const save = canvas.getByRole("button", { name: "Сохранить планирование" }) as HTMLButtonElement;
    await userEvent.click(toggle);
    await userEvent.clear(period);
    await userEvent.type(period, "2");
    await userEvent.click(save);
    await canvas.findByRole("alert");
    await waitFor(() => {
      const model = canvas.getByLabelText("Модель провайдера") as HTMLSelectElement;
      if (save.disabled || !toggle.checked || period.value !== "2" || model.value !== "demo-fast")
        throw new globalThis.Error("Conflict must refresh unchanged settings and retain the planning draft");
    });
    await userEvent.click(save);
    await waitFor(() => {
      canvas.getByText("Настройки планирования заданы вручную.");
      if (!save.disabled || !toggle.checked || period.value !== "2")
        throw new globalThis.Error("Planning retry must save the draft with the current revision");
    });
  },
};
export const StructuredReasoningOff: Story = { name: "Настройки — рассуждения выключены", args: { screen: "Настройки", settingsScenario: "off" } };
export const StructuredReasoningInherited: Story = { name: "Настройки — без переопределения рассуждений", args: { screen: "Настройки", settingsScenario: "inherit" } };
export const ModelsUnavailable: Story = { name: "Настройки — каталог моделей недоступен", args: { screen: "Настройки", settingsScenario: "discovery-error" } };
export const ConfigurationConflict: Story = {
  name: "Настройки — конфликт при сохранении", args: { screen: "Настройки", settingsScenario: "conflict" },
  play: async ({ canvasElement }) => {
    const wait = (predicate: () => boolean) => new Promise<void>((resolve, reject) => {
      if (predicate()) { resolve(); return; }
      const observer = new MutationObserver(() => {
        if (predicate()) { observer.disconnect(); clearTimeout(timer); resolve(); }
      });
      const timer = setTimeout(() => { observer.disconnect(); reject(new globalThis.Error("Settings conflict state not displayed")); }, 3000);
      observer.observe(canvasElement, { subtree: true, childList: true, attributes: true, characterData: true });
    });
    await wait(() => !!canvasElement.querySelector(".agent-model-settings"));
    const form = canvasElement.querySelector<HTMLFormElement>(".agent-model-settings")!;
    if (!form.querySelector<HTMLButtonElement>("button")!.disabled)
      throw new globalThis.Error("An untouched form must not submit an empty settings update");
    const model = form.querySelector<HTMLSelectElement>("select")!;
    const window = form.querySelector<HTMLInputElement>("input")!;
    const reasoning = form.querySelectorAll<HTMLSelectElement>("select")[1];
    reasoning.value = "inherit";
    reasoning.dispatchEvent(new Event("change", { bubbles: true }));
    await wait(() => !form.querySelector(".model-compatibility"));
    form.requestSubmit();
    await wait(() => !!canvasElement.querySelector("[role='alert']"));
    await wait(() => !form.querySelector<HTMLButtonElement>("button")!.disabled);
    if (model.value !== "demo-fast" || window.value !== "65536" || reasoning.value !== "inherit")
      throw new globalThis.Error("Conflict must refresh untouched fields and retain the reasoning draft");
    form.requestSubmit();
    await wait(() => !!canvasElement.querySelector("[role='status']")?.textContent?.includes("Настройки агента сохранены"));
    if (model.value !== "demo-fast" || window.value !== "65536" || reasoning.value !== "inherit")
      throw new globalThis.Error("Retry must preserve the other owner's model and context window");
    if (!form.querySelector<HTMLButtonElement>("button")!.disabled)
      throw new globalThis.Error("Saved drafts must be cleared");
  },
};
export const Cron: Story = { name: "Расписания", args: { screen: "Расписания" } };
export const Workspace: Story = { name: "Файлы workspace", args: { screen: "Файлы workspace" } };
export const Peers: Story = { name: "Переписка агентов", args: { screen: "Переписка агентов" },
  play: async ({ canvasElement }) => {
    await userEvent.click(canvasElement.querySelector(".peer-conversation-card button")!);
    await waitFor(() => {
      if (canvasElement.querySelectorAll(".peer-message").length !== 2)
        throw new globalThis.Error("Both sides of the peer conversation must be loaded");
    });
    const messages = canvasElement.querySelector<HTMLElement>(".peer-messages")!;
    const outgoing = messages.querySelector<HTMLElement>(".peer-outgoing")!;
    const incoming = messages.querySelector<HTMLElement>(".peer-incoming")!;
    for (const message of [outgoing, incoming]) {
      for (const selector of ["h2", "strong", "li", "table", ".code-block code"])
        if (!message.querySelector(`.markdown ${selector}`)) throw new globalThis.Error(`Markdown ${selector} is missing from a conversation direction`);
      const style = getComputedStyle(message);
      if (parseFloat(style.borderTopWidth) < 1 || style.borderTopStyle === "none"
        || style.borderTopColor === "rgba(0, 0, 0, 0)" || parseFloat(style.borderTopLeftRadius) < 12)
        throw new globalThis.Error("Both conversation directions need a visible rounded bubble");
    }
    if (outgoing.querySelector(".peer-message-heading strong")?.textContent !== "Наш агент"
      || outgoing.getBoundingClientRect().left <= incoming.getBoundingClientRect().left
      || messages.scrollWidth > messages.clientWidth + 1)
      throw new globalThis.Error("Outgoing messages must align right and incoming left without panel overflow");
    if (outgoing.querySelector(".markdown a")?.getAttribute("href") !== "https://example.com/orders"
      || messages.querySelector('script, iframe, img[src^="https:"], [onerror]'))
      throw new globalThis.Error("Conversation Markdown must preserve safe links without active HTML or remote images");
  },
};
