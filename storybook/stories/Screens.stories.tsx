import { useMemo, useState, type CSSProperties } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
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
  argTypes: { screen: { control: "select", options: screens }, mode: { control: "select", options: ["normal", "empty", "loading", "error"] }, working: { control: "boolean" }, settingsScenario: { control: "select", options: ["default", "off", "inherit", "discovery-error", "conflict"] } },
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
export const Peers: Story = { name: "Переписка агентов", args: { screen: "Переписка агентов" } };
