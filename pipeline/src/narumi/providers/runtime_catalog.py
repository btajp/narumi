"""Inspect fixed installed dependencies without importing, executing or downloading them."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import uuid
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


class RuntimeInspector:
    """The digest fingerprints distribution metadata, not a verified download archive."""

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
                digest = hashlib.sha256((metadata + "\n" + record).encode()).hexdigest()
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
