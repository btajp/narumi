"""Canonical trust anchor for the Codex runtime shipped by narumi.app."""

CODEX_RUNTIME_LOCK = {
    "version": "0.150.1",
    "source": "https://github.com/openai/codex/releases/tag/rust-v0.150.1",
    "source_tag": "rust-v0.150.1",
    "source_commit": "90854393966b21e9ebfd21b122334eb09a20c93d",
    "artifact": {
        "name": "codex-aarch64-apple-darwin.tar.gz",
        "url": (
            "https://github.com/openai/codex/releases/download/"
            "rust-v0.150.1/codex-aarch64-apple-darwin.tar.gz"
        ),
        "sha256": "f66f1c45f1eda49d6a8aef86faee24121b0c8913cd9023f23ee44262606fc7b6",
        "size": 91484322,
        "entry": "codex-aarch64-apple-darwin",
    },
    "binary": {
        "path": "codex/0.150.1/codex",
        "sha256": "a14f9a907c12c8812878b70e6b7d65f81c39ed795513e46a55817d7428c0ca6b",
        "size": 228986048,
        "architecture": "arm64",
        "version_output": "codex-cli 0.150.1",
        "publisher_team_id": "2DC432GLL2",
    },
    "license": {
        "spdx": "Apache-2.0",
        "path": "licenses/openai-codex-Apache-2.0.txt",
        "source": "https://github.com/openai/codex/blob/rust-v0.150.1/LICENSE",
        "source_tag": "rust-v0.150.1",
        "sha256": "d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc",
        "size": 10926,
        "notice_path": "licenses/openai-codex-NOTICE.txt",
        "notice_source": "https://github.com/openai/codex/blob/rust-v0.150.1/NOTICE",
        "notice_sha256": "9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915",
        "notice_size": 242,
    },
}
