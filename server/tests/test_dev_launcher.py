"""Development launch modes are routed without starting engines or a real server."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev.sh"


@pytest.mark.parametrize("mode", ["http", "stdio", "stdio-bridge"])
def test_dev_launcher_preserves_transport_and_protocol_stdout(tmp_path: Path, mode: str):
    executable = tmp_path / "uv"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    executable.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
        "NARUMI_PORT": "9876",
    }
    environment.pop("GAIA_LIBRARY_CMD", None)
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--" + mode, "--", "--data-root", str(tmp_path / "data")],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    expected = ["run", "narumi-server", "--" + mode]
    if mode == "http":
        expected += ["--port", "9876"]
        assert "https://127.0.0.1:9876/mcp" in completed.stderr
        assert "Keychain token required" in completed.stderr
    else:
        assert "MCP endpoint:" not in completed.stderr
    assert completed.stdout.splitlines() == [*expected, "--data-root", str(tmp_path / "data")]
