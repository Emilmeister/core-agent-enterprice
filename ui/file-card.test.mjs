import { build } from "./node_modules/esbuild/lib/main.js";

const result = await build({ stdin: { contents: `
import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { FileCard } from "./src/FileCard";
import { fileReceipt } from "./src/types";

const original = "/api/chats/chat/tasks/task/attachments/batch/0";
const attachment = {index:0, actual_name:"original.txt", relative_path:"inputs/batch/original.txt",
  size_bytes:8, sha256:"a".repeat(64), download_path:original};
const receipt = {schema_version:1, batch_id:"batch", created_at:1, source:"owner",
  total_bytes:8, entries:[attachment]};
const requests = [], downloads = [];
const api = {session:{valid:true}, request:async path => {
  requests.push(path);
  return new Response(path === original ? "ORIGINAL" : "MODIFIED");
}};
globalThis.document = {createElement:() => ({click() {downloads.push(this.download)}})};
globalThis.window = {setTimeout:callback => callback()};
function button(node, label) {
  if (!node || typeof node !== "object") return;
  if (node.type === "button" && node.props["aria-label"] === label) return node;
  const children = Array.isArray(node.props?.children) ? node.props.children : [node.props?.children];
  return children.flat(Infinity).map(child => button(child, label)).find(Boolean);
}
for (const action of ["Открыть", "Скачать"]) {
  const card = FileCard({api, contextId:"chat", file:attachment});
  const control = button(card, action+" original.txt");
  assert.equal(control.props.disabled, false);
  control.props.onClick();
  await setImmediate();
  assert.equal(requests.at(-1), original, action+" must fetch the immutable original");
}
assert.deepEqual(downloads, ["original.txt"]);
assert.equal(fileReceipt(receipt).entries[0].download_path, original);

for (const path of ["https://other.test/api/file", "//other.test/api/file", "/api/settings", "/api/chats/../tasks/task/attachments/batch/0", original+"?override=1"]) {
  const bad = {...attachment, download_path:path};
  assert.equal(fileReceipt({...receipt, entries:[bad]}), undefined, path);
  for (const action of ["Открыть", "Скачать"])
    assert.equal(button(FileCard({api, contextId:"chat", file:bad}), action+" original.txt").props.disabled, true, path);
}
const {download_path, ...oldInput} = attachment;
for (const action of ["Открыть", "Скачать"])
  assert.equal(button(FileCard({api, contextId:"chat", file:oldInput}), action+" original.txt").props.disabled, true,
    "Old writable input receipt must wait for its authoritative original link");
for (const prefix of ["attachments", "a2a"]) {
  const legacy = {...oldInput, relative_path:prefix+"/batch/original.txt"};
  assert.ok(fileReceipt({...receipt, entries:[legacy]}));
  for (const action of ["Открыть", "Скачать"]) {
    const control = button(FileCard({api, contextId:"chat", file:legacy}), action+" original.txt");
    assert.equal(control.props.disabled, false);
    control.props.onClick();
    await setImmediate();
    assert.equal(requests.at(-1), "/api/chats/chat/files/content?"+new URLSearchParams({path:legacy.relative_path}));
  }
}
console.log("Attachment Open/Download: original, invalid link, pending original and legacy fallback passed");
`, resolveDir: process.cwd(), sourcefile: "file-card-check.tsx", loader: "tsx" },
  bundle: true, platform: "node", format: "esm", write: false, jsx: "automatic",
  nodePaths: [process.cwd() + "/node_modules"],
  // Exercise the component's real action handlers; rendering and state updates
  // are outside this request-routing regression and need no browser dependency.
  plugins: [{name: "card-hooks", setup(build) {
    build.onResolve({filter: /^react$/}, args => args.importer.endsWith("/FileCard.tsx")
      ? {path: "card-hooks", namespace: "card-hooks"} : undefined);
    build.onLoad({filter: /.*/, namespace: "card-hooks"}, () => ({contents:
      "export const useState = value => [value, () => {}]; export const useRef = value => ({current:value}); export const useId = () => 'file-card'; export const useLayoutEffect = () => {};"}));
  }}],
  banner: {js: 'import { createRequire } from "node:module"; const require=createRequire(process.cwd()+"/package.json");'},
});
try {
  await import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64"));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
