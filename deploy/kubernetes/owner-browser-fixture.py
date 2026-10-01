"""Controlled models and transparent service relay for the real browser gate."""
import json
import os
import select
import socket
import socketserver
import sys
import threading


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
    from core_agent.app import create_app
    from core_agent.guardrails import GuardrailClassifier
    from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
    from core_agent.sandbox import SandboxLauncher

    class ClearModel:
        def generate(self, **_kwargs):
            return ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")

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

    class BrowserModel(ScriptedModel):
        def generate(self, *, context, tools, instructions, messages=None):
            index = len(self.calls)
            assert "core_response_files" in tools
            if index:
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
            return super().generate(context=context, tools=tools, instructions=instructions, messages=messages)

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
        ModelResponse(message="Native file reads completed."),
    ])
    model.model = "owner-browser-fixture"
    app = create_app(model=model, guardrail_classifier=GuardrailClassifier(ClearModel()))
    assert isinstance(app.state.core_agent.tool_runtime.environment_manager.backend.launcher, SandboxLauncher)
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    if sys.argv[1:] == ["relay"]:
        relay()
    elif sys.argv[1:] == ["backend"]:
        backend()
    else:
        raise SystemExit("Expected backend or relay")
