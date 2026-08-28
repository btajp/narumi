"""Check the fixed Codex protocol without credentials or external model calls.

Run with ``uv run python scripts/check_codex_protocol.py`` on macOS. The installed
supported binary is copied to a temporary private runtime. The OS sandbox allows
only the fixture port; an allow/deny probe must succeed before Codex starts.
ChatGPT authentication is not tested: this fixture uses an ephemeral fake API key.
"""

from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from narumi.errors import EngineUnavailableError
from narumi.providers.codex import _generation, _policy
from narumi.providers.codex._runtime import CodexRuntime, private_environment
from narumi.providers.codex._session import CodexSession


class FixtureHTTP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    unexpected: list[str] = []
    mode = "success"

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        self.unexpected.append(self.path)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/responses" or not 0 < length <= 8 * 1024 * 1024:
            self.unexpected.append(self.path)
            self.send_error(400)
            return
        if self.headers.get("Content-Encoding") not in (None, "identity"):
            self.unexpected.append("compressed fixture request")
            self.send_error(400)
            return
        self.requests.append(json.loads(self.rfile.read(length)))
        if self.mode == "header_disconnect":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        events = [{"type": "response.created", "response": {"id": "resp-fixture"}}]
        if self.mode == "success":
            events.extend(
                [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "id": "msg-fixture",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "Narumi offline fixture."}],
                        },
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-fixture",
                            "usage": {
                                "input_tokens": 0,
                                "input_tokens_details": None,
                                "output_tokens": 0,
                                "output_tokens_details": None,
                                "total_tokens": 0,
                            },
                        },
                    },
                ]
            )
        payload = "".join(
            "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header(
            "Content-Length", str(len(payload) + (128 if self.mode != "success" else 0))
        )
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self.close_connection = True
        if self.mode != "success":
            self.connection.shutdown(socket.SHUT_RDWR)


class Operation:
    def should_cancel(self) -> bool:
        return False

    def attach(self, _rpc) -> None:
        pass

    def detach(self, _rpc) -> None:
        pass


def check_sandbox(profile: Path, port: int, env: dict[str, str], cwd: Path) -> None:
    with socket.socket() as denied:
        denied.bind(("127.0.0.1", 0))
        denied.listen()
        probe = (
            "import socket,sys; "
            "socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=2).close(); "
            "\ntry:\n socket.create_connection(('127.0.0.1',int(sys.argv[2])),timeout=2)"
            "\nexcept PermissionError:\n print('sandbox_loopback_allow_and_other_port_deny')"
            "\nelse:\n raise SystemExit('sandbox did not deny the other port')\n"
        )
        result = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-f",
                str(profile),
                sys.executable,
                "-I",
                "-c",
                probe,
                str(port),
                str(denied.getsockname()[1]),
            ],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if result.stdout.strip() != "sandbox_loopback_allow_and_other_port_deny":
            raise AssertionError("Sandbox validation failed")
        print(result.stdout.strip(), flush=True)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This check requires the macOS sandbox and a supported Codex installation")
    with tempfile.TemporaryDirectory(prefix="narumi-codex-offline-") as temporary:
        root = Path(temporary).resolve()
        fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHTTP)
        fixture.daemon_threads = True
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            port = fixture.server_port
            profile = root / "network.sb"
            profile.write_text(
                "(version 1)\n(allow default)\n(deny network*)\n"
                f'(allow network-outbound (remote ip "localhost:{port}"))\n'
            )
            for name in ("home", "state", "tmp"):
                (root / name).mkdir(mode=0o700)
            environment = private_environment(root / "home", root / "state", root / "tmp")
            check_sandbox(profile, port, environment, root)
            runtime = CodexRuntime(root)
            resource = runtime.resource()
            runtime.prepare(resource, lambda *_args: None)
            print(
                json.dumps({"runtime_version": resource["version"], "sha256": resource["sha256"]}),
                flush=True,
            )
            settings = copy.deepcopy(_policy.FIXED_CONFIG)
            settings["model_providers"][_policy.MODEL_PROVIDER]["base_url"] = (
                f"http://127.0.0.1:{port}/v1"
            )
            settings["openai_base_url"] = f"http://127.0.0.1:{port}/v1"
            settings["chatgpt_base_url"] = f"http://127.0.0.1:{port}/"
            settings["forced_login_method"] = "api"
            settings["cli_auth_credentials_store"] = "ephemeral"
            original_command = _policy.command

            def sandbox_command(*args, **kwargs):
                return [
                    "/usr/bin/sandbox-exec",
                    "-f",
                    str(profile),
                    *original_command(*args, **kwargs),
                ]

            model = {
                "model_id": "narumi-offline-model",
                "display_name": "Offline fixture",
                "parameter_schema": {
                    "properties": {
                        "reasoning_effort": {
                            "enum": ["medium"],
                            "default": "medium",
                        }
                    }
                },
            }
            with (
                patch.object(_policy, "FIXED_CONFIG", settings),
                patch.object(_policy, "command", sandbox_command),
                patch.object(_generation, "GENERATION_TIMEOUT", 20.0),
            ):
                _policy.host_preflight()
                for mode in ("success", "stream_disconnect", "header_disconnect"):
                    FixtureHTTP.requests = []
                    FixtureHTTP.unexpected = []
                    FixtureHTTP.mode = mode
                    outcome = None
                    try:
                        with CodexSession(
                            runtime, "conn-000000000123", Operation(), model=model
                        ) as session:
                            session.call(
                                "account/login/start",
                                {
                                    "type": "apiKey",
                                    "apiKey": "narumi-offline-test-key",
                                },
                            )
                            result = _generation.generate(
                                session,
                                model,
                                {"reasoning_effort": "medium"},
                                "This is synthetic fixture text, not a meeting.",
                                system=None,
                            )
                            assert mode == "success", "A disconnected response was accepted"
                            assert result == "Narumi offline fixture."
                            outcome = "completed"
                    except EngineUnavailableError as error:
                        if mode == "success":
                            print(
                                json.dumps({"failed_mode": mode, "reason": error.details}),
                                flush=True,
                            )
                            raise
                        assert error.details.get("reason") == "codex_generation_outcome_unknown", (
                            error.details
                        )
                        outcome = "unknown"
                    assert FixtureHTTP.unexpected == [], FixtureHTTP.unexpected
                    assert len(FixtureHTTP.requests) == 1, (mode, len(FixtureHTTP.requests))
                    sent = FixtureHTTP.requests[0]
                    assert sent.get("model") == model["model_id"], sent.get("model")
                    assert sent.get("tools") == [], sent.get("tools")
                    print(
                        json.dumps(
                            {
                                "mode": mode,
                                "outcome": outcome,
                                "http_requests": 1,
                                "tools": [],
                                "model": sent["model"],
                            }
                        ),
                        flush=True,
                    )
        finally:
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    main()
