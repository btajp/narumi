"""Offline inspection of the exact Claude SDK wheel and its bundled CLI."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SDK_VERSION = "0.2.144"
CLI_VERSION = "2.1.239"
EXPECTED_SDK_VERSION = SDK_VERSION
EXPECTED_CLI_VERSION = CLI_VERSION
PACKAGE_NAME = "claude-agent-sdk"
RESOURCE_ID = "claude-agent-sdk-0-2-144"
MAX_CLI_BYTES = 512 * 1024 * 1024
REQUIRED_CLI_CAPABILITIES = (
    "--bare",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--safe-mode",
    "--strict-mcp-config",
    "--tools",
    "CLAUDE_CODE_MAX_RETRIES",
)
ISOLATION_PROFILE_VERSION = "claude-sdk-isolation-v5"
ISOLATION_GUARANTEES = (
    "api-key-and-prompts-over-private-pipes",
    "fixed-public-system-envelope-v1",
    "official-anthropic-endpoint",
    "managed-policy-fail-closed",
    "external-parent-and-worker-death-watchdog",
    "unreaped-worker-pgid-reservation",
    "process-group-term-kill-quiescence",
    "runtime-evidence-request-response-binding",
    "locked-nonprovider-python-dependencies-trust-boundary",
)
_CLI_VERSION_PATTERN = re.compile(
    r'^__cli_version__ = "([0-9]+\.[0-9]+\.[0-9]+)"\s*$', re.MULTILINE
)
PUBLIC_EVIDENCE_FIELDS = frozenset(
    {
        "resource_id",
        "sdk_version",
        "cli_version",
        "cli_sha256",
        "sdk_source_sha256",
        "isolation_profile_sha256",
    }
)
EXECUTION_EVIDENCE_FIELD = "resource_sha256"


@dataclass(frozen=True)
class RuntimeEvidence:
    sdk_version: str
    cli_version: str
    cli_sha256: str
    sdk_source_sha256: str
    isolation_profile_sha256: str
    cli_path: Path

    def public(self) -> dict[str, str]:
        return {
            "resource_id": RESOURCE_ID,
            "sdk_version": self.sdk_version,
            "cli_version": self.cli_version,
            "cli_sha256": self.cli_sha256,
            "sdk_source_sha256": self.sdk_source_sha256,
            "isolation_profile_sha256": self.isolation_profile_sha256,
        }


def inspect_runtime() -> RuntimeEvidence:
    distribution = importlib.metadata.distribution(PACKAGE_NAME)
    if distribution.version != SDK_VERSION:
        raise RuntimeError("unsupported Claude Agent SDK version")
    executable_name = "claude.exe" if os.name == "nt" else "claude"
    cli_relative = Path("claude_agent_sdk") / "_bundled" / executable_name
    cli_path = Path(distribution.locate_file(cli_relative))
    record = distribution.read_text("RECORD")
    if record is None:
        raise RuntimeError("Claude SDK RECORD is unavailable")
    record_hashes = _record_hashes(record)
    cli_digest, _ = _verified_file(
        cli_path,
        MAX_CLI_BYTES,
        required_markers=tuple(item.encode("ascii") for item in REQUIRED_CLI_CAPABILITIES),
    )
    if record_hashes.get(cli_relative.as_posix()) != cli_digest:
        raise RuntimeError("Claude SDK bundled CLI does not match RECORD")
    version_relative = Path("claude_agent_sdk") / "_cli_version.py"
    version_path = Path(distribution.locate_file(version_relative))
    version_digest, version_bytes = _verified_file(version_path, 4096, capture=True)
    if record_hashes.get(version_relative.as_posix()) != version_digest:
        raise RuntimeError("Claude SDK CLI version metadata does not match RECORD")
    assert version_bytes is not None
    version_text = version_bytes.decode("utf-8")
    match = _CLI_VERSION_PATTERN.search(version_text)
    if match is None or match.group(1) != CLI_VERSION:
        raise RuntimeError("unsupported bundled Claude Code version")
    if not os.access(cli_path, os.X_OK):
        raise RuntimeError("Claude SDK bundled CLI is not executable")
    isolation_digest = hashlib.sha256(
        (
            ISOLATION_PROFILE_VERSION
            + "\n"
            + "\n".join(REQUIRED_CLI_CAPABILITIES + ISOLATION_GUARANTEES)
        ).encode("ascii")
    ).hexdigest()
    source_digest = hashlib.sha256()
    runtime_files = sorted(
        relative
        for relative in record_hashes
        if relative.startswith("claude_agent_sdk/") and not relative.endswith("/")
    )
    if not runtime_files:
        raise RuntimeError("Claude SDK distribution contains no verifiable runtime files")
    for relative in runtime_files:
        path = Path(distribution.locate_file(relative))
        digest, _ = _verified_file(path, MAX_CLI_BYTES, allow_empty=True)
        if record_hashes.get(relative) != digest:
            raise RuntimeError("Claude SDK runtime does not match RECORD")
        source_digest.update(relative.encode("ascii") + b"\0" + bytes.fromhex(digest))
    return RuntimeEvidence(
        SDK_VERSION,
        CLI_VERSION,
        cli_digest,
        source_digest.hexdigest(),
        isolation_digest,
        cli_path,
    )


def runtime_evidence() -> dict[str, str]:
    """Return fixed, non-secret values suitable for the runtime resource fingerprint."""
    return inspect_runtime().public()


def runtime_fingerprint(evidence: Mapping[str, str]) -> str:
    """Hash the closed public evidence object for catalog/provenance binding."""
    fields = set(evidence)
    if (
        fields not in {PUBLIC_EVIDENCE_FIELDS, PUBLIC_EVIDENCE_FIELDS | {EXECUTION_EVIDENCE_FIELD}}
        or any(
            not isinstance(value, str)
            or not value
            or not value.isascii()
            or not value.isprintable()
            or len(value) > 128
            or "/" in value
            or "\\" in value
            for value in evidence.values()
        )
        or evidence["sdk_version"] != SDK_VERSION
        or evidence["cli_version"] != CLI_VERSION
        or any(
            re.fullmatch(r"[a-f0-9]{64}", evidence[field]) is None
            for field in ("cli_sha256", "sdk_source_sha256", "isolation_profile_sha256")
        )
        or (
            EXECUTION_EVIDENCE_FIELD in evidence
            and re.fullmatch(r"[a-f0-9]{64}", evidence[EXECUTION_EVIDENCE_FIELD]) is None
        )
    ):
        raise ValueError("invalid Claude SDK runtime evidence")
    canonical = json.dumps(
        dict(evidence),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _record_hashes(record: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in csv.reader(record.splitlines()):
        if len(row) != 3 or not row[1].startswith("sha256="):
            continue
        path, encoded = row[0], row[1].removeprefix("sha256=")
        try:
            digest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
        except (ValueError, TypeError):
            continue
        if len(digest) == 64:
            result[path] = digest
    return result


def _verified_file(
    path: Path,
    limit: int,
    *,
    required_markers: tuple[bytes, ...] = (),
    capture: bool = False,
    allow_empty: bool = False,
) -> tuple[str, bytes | None]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & 0o022
            or not (0 if allow_empty else 1) <= before.st_size <= limit
        ):
            raise RuntimeError("untrusted Claude SDK runtime file")
        digest = hashlib.sha256()
        size = 0
        observed: set[bytes] = set()
        overlap = max((len(marker) for marker in required_markers), default=1) - 1
        tail = b""
        content = bytearray() if capture else None
        while block := os.read(descriptor, min(1024 * 1024, limit - size + 1)):
            size += len(block)
            if size > limit:
                raise RuntimeError("Claude SDK runtime file is too large")
            digest.update(block)
            if content is not None:
                content.extend(block)
            sample = tail + block
            observed.update(marker for marker in required_markers if marker in sample)
            tail = sample[-overlap:] if overlap else b""
        after = os.fstat(descriptor)
        if (
            size != before.st_size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise RuntimeError("Claude SDK runtime changed during inspection")
        if observed != set(required_markers):
            raise RuntimeError("Claude SDK bundled CLI lacks required isolation capabilities")
        return digest.hexdigest(), bytes(content) if content is not None else None
    finally:
        os.close(descriptor)
