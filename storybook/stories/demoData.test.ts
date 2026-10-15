import assert from "node:assert/strict";
import test from "node:test";
import { createDemoRequest, markdown } from "./demoData.ts";

test("demo mutations remain local and unknown routes never call fetch", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("Unexpected network request"); };
  try {
    const first = createDemoRequest(), second = createDemoRequest();
    await first("/api/settings", { method: "PUT", body: JSON.stringify({ hitl_timeout_seconds: 123 }) });
    assert.equal((await (await first("/api/settings")).json()).hitl_timeout_seconds, 123);
    assert.equal((await (await second("/api/settings")).json()).hitl_timeout_seconds, 86400);
    assert.equal((await first("/api/not-configured")).status, 404);
    assert.equal((await first("https://agent.example.invalid/a2a/external/")).status, 404);
    const response = await first("/api/chats/demo/tasks/demo-task/files/report");
    assert.equal(await response.text(), markdown);
    const issued = await first("/api/external-access", { method: "POST", body: JSON.stringify({ name: "New demo", days: 10 }) });
    const token = await issued.json();
    assert.equal(token.access_token, "DEMO-NOT-A-REAL-TOKEN");
    assert.equal(token.expires_in, 864000);
    await first(`/api/external-access/${token.account.id}/account`, { method: "DELETE" });
    assert.equal((await (await first("/api/external-access")).json()).total, 1);
  } finally { globalThis.fetch = original; }
});
test("empty, unavailable and abortable loading states", async () => {
  assert.deepEqual((await (await createDemoRequest("empty")("/api/remote-agents")).json()).agents, []);
  assert.equal((await createDemoRequest("error")("/api/settings")).status, 503);
  const controller = new AbortController();
  const pending = createDemoRequest("loading")("/api/settings", { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
});

test("renaming and archiving survive the App refresh", async () => {
  const request = createDemoRequest();
  await request("/api/chats/demo/title", { method: "PUT", body: JSON.stringify({ title: "Renamed" }) });
  assert.equal((await (await request("/api/chats")).json()).chats[0].title, "Renamed");
  await request("/api/chats/demo", { method: "DELETE" });
  assert.deepEqual((await (await request("/api/chats")).json()).chats, []);
});
