import { useEffect, useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { App } from "../../ui/src/App";
import { createDemoApi } from "./demoApi";
import { createDemoRequest } from "./demoData";
import type { Session } from "../../ui/src/auth";

function Application() {
  const [session, setSession] = useState<Session | null>(null);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    const demo = createDemoApi().session;
    demo.token = async () => "DEMO-NOT-A-REAL-TOKEN";
    demo.logout = async () => { setNotice("Демонстрация выхода: реальная сессия не используется."); };
    const original = window.fetch;
    const request = createDemoRequest();
    // App constructs its own Api. Only this story replaces its HTTP transport.
    // Other stories inject an isolated Api directly into the component.
    window.fetch = async (input, options) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.origin);
      if (url.origin !== location.origin && url.protocol !== "blob:") throw new Error("External requests are disabled in the catalog");
      if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/a2a/")) {
        const init = input instanceof Request ? { method: input.method, signal: input.signal,
          ...(input.method !== "GET" && input.method !== "HEAD" ? { body: await input.clone().text() } : {}), ...options } : options;
        return request(url.pathname + url.search, init);
      }
      return original(input, options);
    };
    setSession(demo);
    return () => { window.fetch = original; demo.valid = false; };
  }, []);
  return <><p className="catalog-note">Полное приложение с настоящей навигацией. Все действия изолированы в этой демонстрации.</p>
    {notice && <p className="success" role="status">{notice}</p>}
    <div className="catalog-screen">{session && <App session={session} />}</div></>;
}
const meta = { title: "Экраны/Полное приложение", component: Application, parameters: { layout: "padded" } } satisfies Meta<typeof Application>;
export default meta;
export const FullApplication: StoryObj<typeof meta> = { name: "Навигация и все разделы" };
