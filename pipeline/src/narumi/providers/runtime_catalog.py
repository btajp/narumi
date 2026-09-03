"""Inspect fixed installed dependencies without importing, executing or downloading them."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
import threading
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.providers._claude_sources import (
    ADAPTER_SOURCE_PATHS,
    RESOURCE_SHA256_FIELD,
    claude_resource_sha256,
)
from narumi.providers._common import timestamp
from narumi.providers._io import _open_directory, _open_regular

RESOURCES = {
    "openai-api": ("openai-client", "narumi", "narumi OpenAI HTTP adapter inspection"),
    "openai-compatible-api": (
        "openai-compatible-client",
        "narumi",
        "narumi OpenAI-compatible HTTP adapter inspection",
    ),
    "anthropic-api": ("anthropic-client", "narumi", "narumi Anthropic HTTP adapter inspection"),
    "claude-agent-sdk": (
        "claude-agent-sdk-0-2-144",
        "claude-agent-sdk",
        "Claude Agent SDK 0.2.144 isolated runtime inspection",
    ),
    "ollama": ("local-ollama", "narumi", "narumi local Ollama HTTP adapter inspection"),
}
# Resolve the trusted import path once; inspection walks never follow symlinks.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_MAX_SOURCE_BYTES = 512 * 1024
_BRIEF_EXECUTION_SOURCES = (
    "brief/__init__.py",
    "brief/builder.py",
    "brief/gaia_context.py",
    "brief/models.py",
)
_TEXT_EXECUTION_SOURCES = (
    *_BRIEF_EXECUTION_SOURCES,
    "bundle/hashing.py",
    "errors.py",
    "generate/bounded.py",
    "generate/checkpoints.py",
    "generate/minutes.py",
    "generate/prompts.py",
    "generate/prompts/minutes_chunk.md",
    "generate/prompts/minutes_final.md",
    "generate/prompts/minutes_reduce.md",
    "llm/base.py",
    "llm/policy.py",
    "llm/registry.py",
    "model_selection.py",
    "models.py",
    "providers/_acl.py",
    "providers/_common.py",
    "providers/_io.py",
    "providers/_requests.py",
    "providers/_runtime_lease.py",
    "providers/auth.py",
    "providers/catalog.py",
    "providers/connections.py",
    "providers/generation.py",
    "providers/runtime.py",
    "providers/runtime_catalog.py",
    "providers/secrets.py",
    "providers/service.py",
    "providers/store.py",
)
_HTTP_EXECUTION_SOURCES = (
    "providers/http_generation.py",
    "providers/http_generation_response.py",
    "providers/metadata/__init__.py",
    "providers/metadata/client.py",
    "providers/metadata/deadline.py",
    "providers/metadata/endpoints.py",
    "providers/metadata/http.py",
    "providers/metadata/tls.py",
    "providers/metadata/validation.py",
)
_OPENAI_AUDIO_SOURCES = (
    "providers/audio_response.py",
    "providers/audio_transcription.py",
    "providers/metadata/audio_capabilities.py",
    "providers/transcription.py",
    "transcribe/__init__.py",
    "transcribe/_checkpoint_format.py",
    "transcribe/_storage.py",
    "transcribe/_wav.py",
    "transcribe/api_stage.py",
    "transcribe/api_transcript.py",
    "transcribe/base.py",
    "transcribe/checkpoints.py",
    "transcribe/chunks.py",
    "transcribe/policy.py",
    "transcribe/registry.py",
    "transcribe/stage.py",
    "transcription_selection.py",
)


def _closed_sources(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Return one deterministic, duplicate-free runtime source inventory."""
    return tuple(sorted({path for group in groups for path in group}))


_OPENAI_API_SOURCES = _closed_sources(
    _TEXT_EXECUTION_SOURCES,
    _HTTP_EXECUTION_SOURCES,
    _OPENAI_AUDIO_SOURCES,
    (
        "providers/metadata/openai.py",
        "providers/metadata/openai_capabilities.py",
    ),
)
_OPENAI_COMPATIBLE_SOURCES = _closed_sources(
    _TEXT_EXECUTION_SOURCES,
    (
        "providers/metadata/__init__.py",
        "providers/metadata/client.py",
        "providers/metadata/deadline.py",
        "providers/metadata/endpoints.py",
        "providers/metadata/http.py",
        "providers/metadata/openai_compatible.py",
        "providers/metadata/openai_compatible_transport.py",
        "providers/metadata/tls.py",
        "providers/metadata/validation.py",
        "providers/openai_compatible.py",
        "providers/openai_compatible_response.py",
    ),
)
_ANTHROPIC_API_SOURCES = _closed_sources(
    _TEXT_EXECUTION_SOURCES,
    _HTTP_EXECUTION_SOURCES,
    ("providers/metadata/anthropic.py",),
)
_OLLAMA_SOURCES = _closed_sources(
    _TEXT_EXECUTION_SOURCES,
    _HTTP_EXECUTION_SOURCES,
    ("providers/metadata/ollama.py",),
)
_CODEX_APP_SERVER_SOURCES = _closed_sources(
    _TEXT_EXECUTION_SOURCES,
    (
        "providers/_codex_auth.py",
        "providers/codex/__init__.py",
        "providers/codex/_generation.py",
        "providers/codex/_models.py",
        "providers/codex/_policy.py",
        "providers/codex/_process_tree.py",
        "providers/codex/_rpc.py",
        "providers/codex/_runtime.py",
        "providers/codex/_runtime_lock.py",
        "providers/codex/_session.py",
        "providers/codex/_supervisor.py",
        "providers/codex/backend.py",
    ),
)
_CLAUDE_ADAPTER_SOURCES = ADAPTER_SOURCE_PATHS
_PROVIDER_SOURCE_SETS = {
    "codex-app-server": _CODEX_APP_SERVER_SOURCES,
    "openai-api": _OPENAI_API_SOURCES,
    "openai-compatible-api": _OPENAI_COMPATIBLE_SOURCES,
    "anthropic-api": _ANTHROPIC_API_SOURCES,
    "claude-agent-sdk": _CLAUDE_ADAPTER_SOURCES,
    "ollama": _OLLAMA_SOURCES,
}


def _source_state(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _source_digest(descriptor: int, directory: int, name: str) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SOURCE_BYTES:
        raise ValueError("Provider runtime source is not a bounded regular file")
    digest = hashlib.sha256()
    consumed = 0
    while block := os.read(descriptor, min(64 * 1024, _MAX_SOURCE_BYTES - consumed + 1)):
        consumed += len(block)
        if consumed > _MAX_SOURCE_BYTES:
            raise ValueError("Provider runtime source exceeds the inspection limit")
        digest.update(block)
    if (
        consumed != before.st_size
        or _source_state(before) != _source_state(os.fstat(descriptor))
        or _source_state(before)
        != _source_state(os.stat(name, dir_fd=directory, follow_symlinks=False))
    ):
        raise ValueError("Provider runtime source changed during inspection")
    return digest.digest()


def _provider_source_digest(relative_paths: tuple[str, ...]) -> bytes:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if not _PACKAGE_ROOT.is_absolute():
        raise ValueError("Provider runtime source root is not absolute")
    with ExitStack() as descriptors:
        anchor = os.open(_PACKAGE_ROOT.anchor, directory_flags)
        descriptors.callback(os.close, anchor)
        directories = []
        sources = []

        def open_child(parent: int, name: str) -> int:
            child = os.open(name, directory_flags, dir_fd=parent)
            descriptors.callback(os.close, child)
            directories.append((parent, name, child))
            return child

        root = anchor
        for part in _PACKAGE_ROOT.parts[1:]:
            root = open_child(root, part)
        opened = {(): root}
        digest = hashlib.sha256()
        for relative_path in relative_paths:
            parts = relative_path.split("/")
            directory = root
            for index, part in enumerate(parts[:-1]):
                prefix = tuple(parts[: index + 1])
                if prefix not in opened:
                    opened[prefix] = open_child(directory, part)
                directory = opened[prefix]
            descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
            try:
                sources.append((directory, parts[-1], _source_state(os.fstat(descriptor))))
                source = _source_digest(descriptor, directory, parts[-1])
            finally:
                os.close(descriptor)
            digest.update(relative_path.encode("ascii") + b"\0" + source)
        for parent, name, state in sources:
            if state != _source_state(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise ValueError("Provider runtime source changed during inspection")
        # Keep all parent descriptors alive so replacements cannot validate in an old tree.
        for parent, name, descriptor in directories:
            if _directory_identity(os.fstat(descriptor)) != _directory_identity(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ):
                raise ValueError("Provider runtime source directory changed during inspection")
        if _directory_identity(os.fstat(anchor)) != _directory_identity(
            os.stat(_PACKAGE_ROOT.anchor, follow_symlinks=False)
        ):
            raise ValueError("Provider runtime source root changed during inspection")
        return digest.digest()


def _openai_source_digest() -> bytes:
    return _provider_source_digest(_PROVIDER_SOURCE_SETS["openai-api"])


def _claude_runtime_digest(evidence: dict[str, str]) -> str:
    """Bind the closed SDK evidence to the exact Narumi Claude adapter sources."""
    from narumi.providers.claude import runtime_fingerprint
    from narumi.providers.claude.snapshot import adapter_source_digest

    sdk_evidence = {key: value for key, value in evidence.items() if key != RESOURCE_SHA256_FIELD}
    return claude_resource_sha256(
        runtime_fingerprint(sdk_evidence),
        adapter_source_digest(package_root=_PACKAGE_ROOT),
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _runtime_digest(provider_id: str, metadata: str, record: str) -> str | None:
    payload = (metadata + "\n" + record).encode()
    if provider_id in _PROVIDER_SOURCE_SETS and provider_id != "claude-agent-sdk":
        try:
            source_digest = _provider_source_digest(_PROVIDER_SOURCE_SETS[provider_id])
        except (OSError, ValueError):
            return None
        payload += f"\0narumi-{provider_id}-sources-v4\0".encode("ascii") + source_digest
    return hashlib.sha256(payload).hexdigest()


class RuntimeInspector:
    """Fingerprint installed metadata and fixed Narumi adapter sources without importing them."""

    def __init__(self) -> None:
        self._claude_lock = threading.Lock()
        self._claude_evidence: dict[str, str] | None = None

    def _inspect_claude(self, *, refresh: bool = False) -> dict[str, str]:
        with self._claude_lock:
            if refresh:
                self._claude_evidence = None
            if self._claude_evidence is None:
                from narumi.providers.claude import runtime_evidence

                self._claude_evidence = runtime_evidence()
            return dict(self._claude_evidence)

    def resource(self, provider_id: str) -> dict[str, Any]:
        if provider_id not in RESOURCES:
            raise InvalidArgumentError("Provider runtime is not supported")
        resource_id, package, display_name = RESOURCES[provider_id]
        version = digest = None
        license_name = "Installed package license metadata unavailable"
        try:
            distribution = importlib.metadata.distribution(package)
            version = distribution.version
            metadata = distribution.read_text("METADATA")
            record = distribution.read_text("RECORD")
            if metadata is not None and record is not None:
                digest = _runtime_digest(provider_id, metadata, record)
            declared = distribution.metadata.get("License-Expression") or (
                distribution.metadata.get("License")
            )
            if declared and declared.strip():
                license_name = declared.strip().splitlines()[0][:160]
        except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
            pass
        if provider_id == "claude-agent-sdk":
            try:
                evidence = self._inspect_claude()

                if evidence.get("resource_id") != resource_id:
                    raise ValueError("Claude runtime resource identity mismatch")
                version = evidence["sdk_version"]
                digest = _claude_runtime_digest(evidence)
            except (
                importlib.metadata.PackageNotFoundError,
                KeyError,
                OSError,
                RuntimeError,
                ValueError,
            ):
                version = digest = None
        return {
            "resource_id": resource_id,
            "display_name": display_name,
            "kind": "runtime",
            "version": version,
            "source": "installed",
            "download_host": None,
            "sha256": digest,
            "license": license_name,
        }

    def expected_runtime(self, provider_id: str, resource: dict[str, Any]) -> dict[str, str] | None:
        """Return the closed Claude evidence only when it matches the catalog resource."""
        if provider_id != "claude-agent-sdk":
            return None
        try:
            evidence = self._inspect_claude()
            if (
                resource.get("resource_id") != evidence["resource_id"]
                or resource.get("version") != evidence["sdk_version"]
                or resource.get("sha256") != _claude_runtime_digest(evidence)
            ):
                raise ValueError("Claude runtime evidence does not match the catalog resource")
        except (
            importlib.metadata.PackageNotFoundError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            raise EngineUnavailableError(
                "Claude Agent SDK runtime evidence does not match the prepared runtime"
            ) from None
        return {**evidence, RESOURCE_SHA256_FIELD: resource["sha256"]}

    @staticmethod
    def catalog_revision(resource: dict[str, Any]) -> str:
        payload = json.dumps(resource, sort_keys=True).encode()
        resource_id = resource.get("resource_id")
        if isinstance(resource_id, str) and resource_id.startswith("codex-"):
            try:
                source_digest = _provider_source_digest(_CODEX_APP_SERVER_SOURCES)
            except (OSError, ValueError):
                # The executable-only resource is insufficient: generation and retry
                # behavior also lives in Python. Never publish a synthetic replacement
                # identity which could later be prepared as if it represented exact code.
                raise EngineUnavailableError(
                    "Codex adapter source inventory is unavailable"
                ) from None
            payload += b"\0narumi-codex-app-server-sources-v5\0" + source_digest
        return hashlib.sha256(payload).hexdigest()

    def prepare(
        self,
        root: Path,
        provider_id: str,
        resource: dict[str, Any],
        progress: Any,
    ) -> None:
        progress("inspect_installed_runtime", 0.25)
        if provider_id == "claude-agent-sdk":
            try:
                self._inspect_claude(refresh=True)
            except (
                importlib.metadata.PackageNotFoundError,
                OSError,
                RuntimeError,
                ValueError,
            ):
                raise EngineUnavailableError(
                    "Claude Agent SDK isolation evidence is unavailable"
                ) from None
        current = self.resource(provider_id)
        if current != resource:
            raise EngineUnavailableError("Provider runtime changed during preparation")
        if current["version"] is None:
            raise EngineUnavailableError("Provider runtime dependency is not installed")
        if (
            provider_id
            in (
                "openai-api",
                "openai-compatible-api",
                "anthropic-api",
                "claude-agent-sdk",
                "ollama",
            )
            and current["sha256"] is None
        ):
            raise EngineUnavailableError("Provider runtime distribution metadata is incomplete")
        expected_runtime = self.expected_runtime(provider_id, current)
        directory = _open_directory(
            root / "providers" / "runtime" / provider_id,
            trusted_root=root,
        )
        temporary = f".inspection.{uuid.uuid4().hex}.tmp"
        try:
            progress("prepare_private_runtime_directory", 0.6)
            descriptor = _open_regular(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(
                    json.dumps(
                        {
                            "provider_id": provider_id,
                            "resource": current,
                            "inspection_kind": "installed_distribution_metadata",
                            "checked_at": timestamp(),
                            "sdk_execution_verified": provider_id == "claude-agent-sdk",
                            "runtime_evidence": expected_runtime,
                        },
                        sort_keys=True,
                    ).encode()
                )
                stream.flush()
                os.fsync(stream.fileno())
            # Do not replace a hostile pre-existing object, even though no secret lives here.
            try:
                previous = _open_regular(directory, "inspection.json", os.O_RDONLY)
            except FileNotFoundError:
                pass
            else:
                os.close(previous)
            os.replace(temporary, "inspection.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            progress("runtime_inspection_complete", 0.9)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
