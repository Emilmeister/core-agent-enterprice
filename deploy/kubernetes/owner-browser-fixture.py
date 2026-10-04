"""Controlled models and transparent service relay for the real browser gate."""
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def relay():
    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(self.server.target, timeout=10) as upstream:
                upstream.settimeout(None)
                peers = (self.request, upstream)
                while True:
                    ready, _, _ = select.select(peers, (), (), 120)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        peers[source is self.request].sendall(data)

    servers = []
    for port, name in ((49737, "BROWSER_KEYCLOAK_TARGET"), (49738, "BROWSER_POSTGRES_TARGET")):
        host, target_port = os.environ[name].rsplit(":", 1)
        server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Forward)
        server.daemon_threads = True
        server.target = host, int(target_port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    threading.Event().wait()


def backend():
    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from core_agent.app import create_app
    from core_agent.guardrails import GuardrailClassifier
    from core_agent.model import CompatibleHttpModel, ModelResponse, ScriptedModel, ToolRequest
    from core_agent.sandbox import SandboxLauncher

    class ClearModel:
        def generate(self, **kwargs):
            verdict = "suspicious" if "Browser guardrail review proof" in kwargs.get("context", "") else "clear"
            return ModelResponse(message=json.dumps({"verdict": verdict}), finish_reason="stop")

        def count_tokens(self, text):
            return max(1, len(text.encode()) // 3)

    def read_files(count, marker):
        # These assertions execute through the actual registered terminal tool.
        code = f"""import errno, os, pathlib
root = pathlib.Path('/workspace/attachments')
files = sorted(root.rglob('*.txt'))
assert {count} <= len(files) <= 3, 'wrong published file count'
expected = {{b'first report', b'second report'}}
if {count} == 3: expected.add(b'followup report')
observed = {{p.read_bytes() for p in files}}
assert expected <= observed <= {{b'first report', b'second report', b'followup report'}}, 'wrong mounted bytes'
assert 'KEYCLOAK_CLIENT_SECRET' not in os.environ
assert 'DATABASE_URL' not in os.environ
for p in files:
    try: p.open('wb')
    except OSError as error: assert error.errno in (errno.EROFS, errno.EACCES)
    else: raise AssertionError('published input is writable')
if {count} == 3:
    output = root.parent / 'results'
    output.mkdir()
    (output / 'report-output.txt').write_bytes(b'Native immutable output\\n')
    (output / 'empty.txt').write_bytes(b'')
print({marker!r})
"""
        return ModelResponse(tool_requests=(ToolRequest(marker, "core_terminal_exec", {"argv": ["python3", "-P", "-c", code]}),))

    peer_release = threading.Event()
    peer_state = {"sends": 0, "gets": 0, "message": None, "models_available": True}
    peer_url = "http://127.0.0.1:8001/a2a/"

    class PublicPeer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/models":
                assert self.headers.get("Authorization") == "Bearer browser-provider-credential"
                return self.send_json({"data": [{"id": "owner-browser-fixture"}, {"id": "browser-model-two"}]},
                                      200 if peer_state["models_available"] else 503)
            if self.path.endswith("/.well-known/agent-card.json"):
                return self.send_json({"name": "browser-peer", "description": "Native public A2A peer",
                    "supportedInterfaces": [{"url": peer_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
                    "capabilities": {"streaming": False}, "skills": []})
            return self.send_json({}, 404)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/a2a/" and self.headers.get("Authorization") == "Bearer browser-peer-credential"
            method = payload["method"]
            if method == "SendMessage":
                peer_state["sends"] += 1
                peer_state["message"] = payload["params"]["message"]
            else:
                assert method == "GetTask", "Unexpected remote mutation"
                peer_state["gets"] += 1
            messages = [{"messageId": "peer-accepted", "role": "ROLE_AGENT", "parts": [{"text": "Поручение принято."}]}]
            if peer_state["gets"]:
                messages.append({"messageId": "peer-progress", "role": "ROLE_AGENT", "parts": [
                    {"text": "Проверяю данные для поручения.\n\n" + "\n".join(f"- Пункт проверки {index}" for index in range(55))},
                    {"text": "private-peer-reasoning", "metadata": {"kind": "reasoning"}}]})
            task = {"id": "browser-public-peer-task", "contextId": "browser-public-peer-context",
                    "status": {"state": "TASK_STATE_COMPLETED" if peer_release.is_set() else "TASK_STATE_WORKING"},
                    "history": messages}
            if peer_release.is_set():
                task["artifacts"] = [{"artifactId": "peer-result", "parts": [
                    {"text": "**Проверка завершена.** Отчёт подготовлен."},
                    {"filename": "peer-report.txt", "mediaType": "text/plain", "raw": base64.b64encode(b"Native peer report\n").decode()}]}]
            self.send_json({"jsonrpc": "2.0", "id": payload["id"], "result": {"task": task} if method == "SendMessage" else task})

    peer_server = ThreadingHTTPServer(("127.0.0.1", 8001), PublicPeer)
    threading.Thread(target=peer_server.serve_forever, daemon=True).start()

    class BrowserModel(ScriptedModel, CompatibleHttpModel):
        def __init__(self, responses):
            super().__init__(responses)
            CompatibleHttpModel.__init__(self, api_format="openai", model="owner-browser-fixture",
                                         base_url="http://127.0.0.1:8001/v1", api_key="browser-provider-credential", stream=True)
            self.reply_started = threading.Event()
            self.reply_release = threading.Event()
            self.reply_finished = threading.Event()
            self.reply_waiting = threading.Event()

        def generate(self, *, context, tools, instructions, messages=None, on_delta=None):
            index = len(self.calls)
            if index == 15:
                assert self.model == "browser-model-two", "new root did not use owner model selection"
                assert "Native owner profile proof" in str(instructions), "new root did not use owner profile"
            if index == 14:
                assert not tools, "public answer turn still exposes tools"
                assert on_delta is not None, "model streaming callback is absent"
                response = super().generate(context=context, tools=tools, instructions=instructions, messages=messages)
                on_delta("# Native streamed reply\n\n```python\nprint('live", "private-native-reasoning")
                self.reply_started.set()
                self.reply_waiting.set()
                try:
                    assert self.reply_release.wait(120), "browser did not release the blocked provider"
                finally:
                    self.reply_waiting.clear()
                on_delta(response.message, "private-native-reasoning")
                self.reply_finished.set()
                return response
            assert "core_response_files" in tools
            if index == 13:
                assert "core_response_begin" in tools, "public answer signal is absent"
            if index == 0:
                assert "core_cron_create" in tools
            else:
                assert "core_cron_create" not in tools, "owner deny did not narrow actual model catalog"
            if 1 <= index <= 4:
                marker = ("browser-native-root-verified", "browser-native-followup-verified",
                          "browser-native-output-selected", "browser-native-output-deleted")[index - 1]
                result = next(json.loads(message["content"]) for message in messages
                              if message.get("role") == "tool" and message.get("tool_call_id") == marker)
                assert result["status"] == "succeeded"
                if marker == "browser-native-output-selected":
                    assert [file["name"] for file in result["output"]["files"]] == ["report-output.txt", "empty.txt"]
                    assert result["output"]["files"][1]["size_bytes"] == 0
                else:
                    assert result["output"]["exit_code"] == 0
                    assert result["output"]["stdout"].strip() == marker, "actual native file operation did not succeed"
            if index == 5:
                assert "Native file reads completed." in context, "manual cron lost original chat history"
            response = super().generate(context=context, tools=tools, instructions=instructions, messages=messages)
            if index == 15:
                attached = next(message["content"] for message in reversed(messages)
                                if message.get("role") == "user" and "Attached files: " in message.get("content", ""))
                folder = attached.rsplit("; folder: /workspace/", 1)[1].strip()
                response = ModelResponse(tool_requests=(ToolRequest("browser-peer-send", "core_agent_send_message", {
                    "agent_name": "browser-peer", "task": "Подготовь краткий отчёт по поручению.",
                    "files": [folder + "/report.txt"]}),))
            if response.tool_requests and response.tool_requests[0].id in {"browser-background-wait", "browser-peer-wait"}:
                waiting = response.tool_requests[0].id
                source = "browser-peer-send" if waiting == "browser-peer-wait" else "browser-background-failure"
                admitted = next(json.loads(message["content"]) for message in messages
                                if message.get("tool_call_id") == source)
                assert admitted["status"] == "succeeded"
                response = ModelResponse(tool_requests=(ToolRequest(waiting, "core_task_wait",
                    {"task_id": admitted["output"]["task_id"]}),))
            return response

    preview_python = "value = 7\nif value:\n    print('python highlight verified')\n"
    preview_markdown = ("# File preview proof\n\n| Item | Status |\n| --- | --- |\n| file | ready |\n\n"
        "```python\n" + preview_python + "```\n\n```mermaid\nflowchart LR\n    A[File] --> B[Preview]\n```\n\n"
        '<script>window.__fileExecuted=true</script>\n![tracking](https://ui-content.invalid/preview)\n')
    unsafe_file = '<script>window.__fileExecuted=true</script><img src="https://ui-content.invalid/file">'
    preview_files = {"unsafe.html": unsafe_file, "unsafe.svg": unsafe_file, "safe-preview.txt": unsafe_file,
        "preview.md": preview_markdown, "example.py": preview_python, "long-preview.txt": "preview line\n" * 1000}
    preview_program = ("import sys\nfrom pathlib import Path\np=Path('/workspace/preview-proof')\np.mkdir(exist_ok=True)\n"
        f"files={preview_files!r}\nfor name, content in files.items():\n    (p/name).write_text(content)\n"
        "print('native known failure', file=sys.stderr)\nsys.exit(3)\n")
    model = BrowserModel([
        read_files(2, "browser-native-root-verified"),
        read_files(3, "browser-native-followup-verified"),
        ModelResponse(tool_requests=(ToolRequest("browser-native-output-selected", "core_response_files",
            {"paths": ["results/report-output.txt", "results/empty.txt"]}),)),
        ModelResponse(tool_requests=(ToolRequest("browser-native-output-deleted", "core_terminal_exec", {
            "argv": ["python3", "-P", "-c", "from pathlib import Path\np=Path('/workspace/results/report-output.txt')\n"
                     "assert p.read_bytes()==b'Native immutable output\\n'\np.write_bytes(b'changed after selection')\n"
                     "assert p.read_bytes()==b'changed after selection'\np.unlink()\n"
                     "q=Path('/workspace/results/empty.txt')\nassert q.read_bytes()==b''\nq.unlink()\n"
                     "assert not p.exists() and not q.exists()\nprint('browser-native-output-deleted')"],
        }),)),
        ModelResponse(message="""Native file reads completed.

## Formatted response

**Verified** attachment reads.

- first
- second

| File | Status |
| --- | --- |
| report | ready |

```python
print('ready')
```

```mermaid
flowchart LR
    A[Request] --> B[Answer]
    click A "https://ui-content.invalid/mermaid"
```

```mermaid
flowchart LR
    A -->
```

<script>window.__markdownExecuted = true</script>
<img src="https://ui-content.invalid/html" onerror="window.__markdownExecuted = true">
![tracking](https://ui-content.invalid/image)
[unsafe](javascript:alert('unsafe'))
"""),
        ModelResponse(message="Native manual cron completed."),
        ModelResponse(tool_requests=(ToolRequest("browser-owner-question", "core_ask_owner", {"question": "Какой номер заказа использовать?"}),)),
        ModelResponse(tool_requests=(ToolRequest("browser-python-syntax", "core_python_exec", {"code": preview_python}),)),
        ModelResponse(tool_requests=(ToolRequest("browser-known-failure", "core_terminal_exec", {"argv": ["python3", "-c", preview_program]}),)),
        ModelResponse(tool_requests=(
            ToolRequest("browser-background-failure", "core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["python3", "-c", "print('should not run')"], "cwd": "definitely-missing-browser-folder"}}),
            # The explicit wait acknowledges the first job. A separate optional
            # job produces the real notification marker exercised by history UI.
            ToolRequest("browser-background-notification", "core_task_start", {"tool": "core_terminal_exec", "required": False, "arguments": {"argv": ["python3", "-c", "print('should not run')"], "cwd": "another-missing-browser-folder"}}),
        )),
        ModelResponse(tool_requests=(ToolRequest("browser-background-wait", "core_task_wait", {"task_id": "from-actual-admission"}),)),
        ModelResponse(tool_requests=(ToolRequest("browser-preview-files", "core_response_files", {"paths": ["preview-proof/" + name for name in preview_files]}),)),
        ModelResponse(message="Owner clarification received; the command failed with exit code 3."),
        ModelResponse(tool_requests=(ToolRequest("browser-public-reply", "core_response_begin", {}),)),
        ModelResponse(message="# Native streamed reply\n\n```python\nprint('live reply complete')\n```\n\nNative streaming completed."),
        ModelResponse(tool_requests=(ToolRequest("browser-peer-send", "core_agent_send_message", {
            "agent_name": "browser-peer", "task": "Подготовь краткий отчёт по поручению."}),)),
        ModelResponse(tool_requests=(ToolRequest("browser-peer-wait", "core_task_wait", {"task_id": "from-actual-admission"}),)),
        ModelResponse(message="Ответ внешнего агента и отчёт получены."),
    ])
    model.model = "owner-browser-fixture"
    app = create_app(model=model, guardrail_classifier=GuardrailClassifier(ClearModel()))
    async def reply_fixture(request):
        # Test-only control, protected by the real application's owner middleware.
        if request.method == "POST":
            model.reply_release.set()
        return JSONResponse({"started": model.reply_started.is_set(), "finished": model.reply_finished.is_set(),
                             "waiting": model.reply_waiting.is_set()},
                            headers={"Cache-Control": "no-store"})

    app.routes.append(Route("/api/browser-reply-fixture", reply_fixture, methods=["GET", "POST"]))
    async def peer_fixture(request):
        if request.method == "POST":
            payload = await request.json()
            if payload.get("finish"):
                peer_release.set()
            if "models_available" in payload:
                peer_state["models_available"] = payload["models_available"]
        return JSONResponse({"sends": peer_state["sends"], "gets": peer_state["gets"], "finished": peer_release.is_set(),
                             "now": time.time()}, headers={"Cache-Control": "no-store"})
    app.routes.append(Route("/api/browser-peer-fixture", peer_fixture, methods=["GET", "POST"]))
    assert isinstance(app.state.core_agent.tool_runtime.environment_manager.backend.launcher, SandboxLauncher)
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    if sys.argv[1:] == ["relay"]:
        relay()
    elif sys.argv[1:] == ["backend"]:
        backend()
    else:
        raise SystemExit("Expected backend or relay")
