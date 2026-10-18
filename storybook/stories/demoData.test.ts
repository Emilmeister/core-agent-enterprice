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

test("context window can be set and reset in the settings demo", async () => {
  const request = createDemoRequest();
  const saved = await (await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: 1, context_window: 65536 }),
  })).json();
  assert.equal(saved.context_window, 65536);
  assert.equal(saved.inherits.context_window, false);
  const reset = await (await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: 2, context_window: null }),
  })).json();
  assert.equal(reset.context_window, 128000);
  assert.equal(reset.inherits.context_window, true);
});

test("structured settings preserve unrelated values, reject stale saves and reset to deployment", async () => {
  const request = createDemoRequest();
  const structured_output = { reasoning: "off", compatibility: "chat_template" };
  const saved = await (await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: 1, structured_output }),
  })).json();
  assert.deepEqual(saved.structured_output, structured_output);
  assert.equal(saved.inherits.structured_output, false);
  const unrelated = await (await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: saved.revision, profile_prompt: "Новый профиль" }),
  })).json();
  assert.deepEqual(unrelated.structured_output, structured_output);
  assert.equal((await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: saved.revision, structured_output: null }),
  })).status, 409);
  const reset = await (await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: unrelated.revision, model_id: null, context_window: null, structured_output: null }),
  })).json();
  assert.deepEqual(reset.structured_output, { reasoning: "auto", compatibility: "auto" });
  assert.equal(reset.inherits.structured_output, true);
  assert.equal(reset.profile_prompt, "Новый профиль");
});

test("settings scenarios allow changes without discovery and retry after a conflict", async () => {
  const request = createDemoRequest("normal", undefined, "discovery-error");
  assert.equal((await request("/api/agent-settings/models")).status, 503);
  const saved = await (await request("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: 1, structured_output: { reasoning: "inherit", compatibility: "thinking" } }),
  })).json();
  assert.equal(saved.structured_output.reasoning, "inherit");
  const conflict = createDemoRequest("normal", undefined, "conflict");
  assert.equal((await conflict("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: 1, structured_output: saved.structured_output }),
  })).status, 409);
  const refreshed = await (await conflict("/api/agent-settings")).json();
  assert.deepEqual(refreshed.structured_output, { reasoning: "auto", compatibility: "auto" });
  assert.equal(refreshed.model_id, "demo-fast");
  assert.equal(refreshed.context_window, 65536);
  const retried = await (await conflict("/api/agent-settings", {
    method: "PUT", body: JSON.stringify({ expected_revision: refreshed.revision, structured_output: saved.structured_output }),
  })).json();
  assert.equal(retried.model_id, "demo-fast");
  assert.equal(retried.context_window, 65536);
  assert.deepEqual(retried.structured_output, saved.structured_output);
});
