"""Inspect fixed installed dependencies without importing, executing or downloading them."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.providers._common import timestamp
from narumi.providers._io import _open_directory, _open_regular

RESOURCES = {
    "openai-api": ("openai-client", "narumi", "narumi OpenAI HTTP adapter inspection"),
    "anthropic-api": ("anthropic-client", "narumi", "narumi Anthropic HTTP adapter inspection"),
    "claude-agent-sdk": ("claude-sdk", "claude-agent-sdk", "Claude installed SDK inspection"),
    "ollama": ("local-ollama", "narumi", "narumi local Ollama HTTP adapter inspection"),
}
# Resolve the trusted import path once; inspection walks never follow symlinks.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_MAX_SOURCE_BYTES = 512 * 1024
_OPENAI_AUDIO_SOURCES = (
    "providers/audio_transcription.py",
    "providers/audio_response.py",
    "providers/metadata/audio_capabilities.py",
    "providers/metadata/http.py",
    "providers/metadata/deadline.py",
    "providers/metadata/tls.py",
    "providers/metadata/validation.py",
    "providers/transcription.py",
    "transcription_selection.py",
)


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


def _openai_source_digest() -> bytes:
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
        for relative_path in _OPENAI_AUDIO_SOURCES:
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


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _runtime_digest(provider_id: str, metadata: str, record: str) -> str | None:
    payload = (metadata + "\n" + record).encode()
    if provider_id == "openai-api":
        try:
            source_digest = _openai_source_digest()
        except (OSError, ValueError):
            return None
        payload += b"\0narumi-openai-audio-sources-v1\0" + source_digest
    return hashlib.sha256(payload).hexdigest()


class RuntimeInspector:
    """Fingerprint distribution metadata and fixed OpenAI sources, not a download archive."""

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

    @staticmethod
    def catalog_revision(resource: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()

    def prepare(
        self,
        root: Path,
        provider_id: str,
        resource: dict[str, Any],
        progress: Any,
    ) -> None:
        progress("inspect_installed_runtime", 0.25)
        current = self.resource(provider_id)
        if current != resource:
            raise EngineUnavailableError("Provider runtime changed during preparation")
        if current["version"] is None:
            raise EngineUnavailableError("Provider runtime dependency is not installed")
        if provider_id in ("openai-api", "anthropic-api", "ollama") and current["sha256"] is None:
            raise EngineUnavailableError("Provider runtime distribution metadata is incomplete")
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
                            "sdk_execution_verified": False,
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
