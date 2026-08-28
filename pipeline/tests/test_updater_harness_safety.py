"""The legacy updater harness must stop before touching user state or build tools."""

import subprocess
from pathlib import Path


def test_legacy_harness_stops_before_any_external_tool_or_cleanup(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    work = tmp_path / "existing-app-data"
    work.mkdir()
    sentinel = work / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    result = subprocess.run(
        ["/bin/bash", str(root / "app/e2e-updater/run-e2e.sh")],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path / "no-external-tools"),
            "E2E_DIR": str(work),
            "SPARKLE_BIN": str(tmp_path / "no-sparkle-tools"),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "現在は実行停止中" in result.stderr
    assert "command not found" not in result.stderr
    assert result.stdout == ""
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert set(tmp_path.iterdir()) == {work}
    assert set(work.iterdir()) == {sentinel}
