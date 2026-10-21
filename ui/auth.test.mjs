import { build } from "./node_modules/esbuild/lib/main.js";
const result = await build({stdin: {contents: `
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { Session, signIn } from "./src/auth";
import { Api } from "./src/api";
import { ExternalAccess } from "./src/ExternalAccess";
const identity = {actor_id:"owner", role:"owner", tenant:"company"};
const config = {auth_mode:"cloudru", external_a2a_url:"https://agent.test/a2a/external/"};
const requests = [];
let authenticated = false, unauthorized = false, logoutFails = false, redirected = "";
globalThis.location = {origin:"https://agent.test", replace: value => {redirected=value}};
globalThis.localStorage = globalThis.sessionStorage = new Proxy({}, {get() {throw Error("No browser storage")}});
globalThis.fetch = async (path, options={}) => {
  requests.push({path, options});
  const headers = new Headers(options.headers);
  if (path === "/ui/config") return Response.json(config);
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.redirect, "error");
  assert.equal(headers.has("Authorization"), false);
  if (path === "/api/auth/login") {
    assert.equal(options.method, "POST");
    assert.equal(headers.get("Content-Type"), "application/json");
    authenticated = JSON.parse(options.body).password === "owner-password";
    return Response.json({status:"ok"}, {status:authenticated ? 200 : 401});
  }
  if (path === "/api/identity") return Response.json(identity, {status:authenticated ? 200 : 401});
  if (path === "/api/auth/logout") {
    assert.equal(options.method, "POST");
    if (logoutFails) return new Response(null, {status:503});
    authenticated = false;
  }
  if (unauthorized) return Response.json({error:{code:"AUTH_REQUIRED"}}, {status:401});
  if (path.endsWith(":subscribe")) return new Response('data: {"id":"event"}\\n\\n', {
    headers:{"Content-Type":"text/event-stream"},
  });
  return Response.json({ok:true});
};
await assert.rejects(signIn(), error => error.passwordRequired === true);
assert.deepEqual(requests.map(row => row.path), ["/ui/config", "/api/identity"]);
await assert.rejects(signIn("wrong-password"), error => error.passwordRequired === true);
const session = await signIn("owner-password");
assert.equal(session.authMode, "cloudru");
assert.deepEqual(session.identity, identity);
const recovered = await signIn();
assert.equal(recovered.authMode, "cloudru");
const api = new Api(session);
await api.json("/api/agent-settings", "PUT", {revision:1});
await api.json("/a2a/owner/message:send", "POST", {message:{parts:[{file:{bytes:"YQ=="}}]}});
await api.request("/api/chats/chat/files/file/download");
const events=[];
await api.subscribe("task", async event => events.push(event), new AbortController().signal);
assert.deepEqual(events, [{id:"event"}]);
await api.request("/api/chats", {headers:{Authorization:"Bearer must-not-leak"}});
await assert.rejects(api.request("https://elsewhere.test/api/private"));
const access = renderToStaticMarkup(<ExternalAccess api={api}/>);
for (const text of ["https://agent.test/a2a/external/", "Cloud.ru", "Evolution AI Agents", "сервисный аккаунт", "отдельный", "выключить", "удалить"])
  assert.ok(access.includes(text), text);
assert.ok(!access.includes("Выдать доступ"));
assert.ok(!access.includes("Выдать новый токен"));
let expired = 0;
session.onExpired = () => expired++;
unauthorized = true;
await assert.rejects(api.json("/api/chats"));
assert.equal(expired, 1);
assert.equal(session.valid, false);
const before = requests.length;
await assert.rejects(api.json("/api/chats"));
assert.equal(requests.length, before);
unauthorized = false;
logoutFails = true;
await assert.rejects(recovered.logout());
assert.equal(redirected, "");
logoutFails = false;
await recovered.logout();
assert.equal(recovered.valid, false);
assert.equal(redirected, "/ui/?logged_out=1");
await assert.rejects(signIn(), error => error.passwordRequired === true);
let refreshed = 0;
const keycloak = {token:"keycloak-token", updateToken:async () => refreshed++, clearToken:()=>{}};
const oldSession = new Session(keycloak, identity);
globalThis.fetch = async (path, options) => {
  assert.equal(new Headers(options.headers).get("Authorization"), "Bearer keycloak-token");
  assert.equal(options.credentials, "omit");
  return Response.json({});
};
await new Api(oldSession).json("/api/chats");
assert.equal(refreshed, 1);
assert.equal(oldSession.authMode, "keycloak");
`, resolveDir:process.cwd(), sourcefile:"cloudru-ui-check.tsx", loader:"tsx"},
  bundle:true, platform:"node", format:"esm", write:false,
  nodePaths:[process.cwd()+"/node_modules"], jsx:"automatic",
  banner:{js:'import { createRequire } from "node:module"; const require=createRequire(process.cwd()+"/package.json");'}});
try {
  await import("data:text/javascript;base64,"+Buffer.from(result.outputFiles[0].text).toString("base64"));
} catch (error) {
  console.error(error.message);
  process.exitCode=1;
}
