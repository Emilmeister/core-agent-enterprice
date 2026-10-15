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
import type { DemoMode } from "./demoData";
import { demoPeer, demoRow } from "./demoData";
import { createDemoApi } from "./demoApi";

const noop = () => {};
const done = async () => {};
const screens = ["Чат", "Инструменты", "Внешние агенты", "Доступ к агенту", "Настройки", "Расписания", "Файлы workspace", "Переписка агентов"] as const;
type Screen = typeof screens[number];
function ScreenExample({ screen, mode, working }: { screen: Screen; mode: DemoMode; working: boolean }) {
  const [reset, setReset] = useState(0);
  return <div className="catalog">
    <p className="catalog-note">Демонстрационный экран. Настройки, удаления, токены и сообщения существуют только в памяти этой страницы.</p>
    <button className="secondary catalog-reset" onClick={() => setReset(value => value + 1)}>Сбросить демоданные</button>
    <ScreenContent key={`${screen}:${mode}:${working}:${reset}`} screen={screen} mode={mode} working={working} />
  </div>;
}
function ScreenContent({ screen, mode, working }: { screen: Screen; mode: DemoMode; working: boolean }) {
  const api = useMemo(() => createDemoApi(mode, working ? "TASK_STATE_WORKING" : undefined), [mode, working]);
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
  args: { screen: "Чат", mode: "normal", working: false },
  argTypes: { screen: { control: "select", options: screens }, mode: { control: "select", options: ["normal", "empty", "loading", "error"] }, working: { control: "boolean" } },
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
export const Cron: Story = { name: "Расписания", args: { screen: "Расписания" } };
export const Workspace: Story = { name: "Файлы workspace", args: { screen: "Файлы workspace" } };
export const Peers: Story = { name: "Переписка агентов", args: { screen: "Переписка агентов" } };
