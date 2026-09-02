"""Closed source set and digest domain shared by Claude discovery and execution."""

from __future__ import annotations

import hashlib

RESOURCE_SHA256_FIELD = "resource_sha256"
RESOURCE_DOMAIN = b"narumi-claude-agent-sdk-runtime-v3\0"

ADAPTER_SOURCE_PATHS = (
    "__init__.py",
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
    "providers/__init__.py",
    "providers/_claude_sources.py",
    "providers/_acl.py",
    "providers/_common.py",
    "providers/_io.py",
    "providers/_requests.py",
    "providers/_runtime_lease.py",
    "providers/auth.py",
    "providers/catalog.py",
    "providers/claude/__init__.py",
    "providers/claude/backend.py",
    "providers/claude/protocol.py",
    "providers/claude/runtime.py",
    "providers/claude/snapshot.py",
    "providers/claude/transport.py",
    "providers/claude/worker.py",
    "providers/claude_sdk_backend.py",
    "providers/connections.py",
    "providers/generation.py",
    "providers/metadata/__init__.py",
    "providers/metadata/anthropic.py",
    "providers/metadata/client.py",
    "providers/metadata/deadline.py",
    "providers/metadata/endpoints.py",
    "providers/metadata/http.py",
    "providers/metadata/ollama.py",
    "providers/metadata/openai.py",
    "providers/metadata/openai_capabilities.py",
    "providers/metadata/openai_compatible.py",
    "providers/metadata/openai_compatible_transport.py",
    "providers/metadata/tls.py",
    "providers/metadata/validation.py",
    "providers/runtime.py",
    "providers/runtime_catalog.py",
    "providers/secrets.py",
    "providers/service.py",
    "providers/store.py",
)


def claude_resource_sha256(evidence_fingerprint: str, source_digest: bytes) -> str:
    if len(evidence_fingerprint) != 64 or not isinstance(source_digest, bytes):
        raise ValueError("invalid Claude runtime digest input")
    try:
        evidence_digest = bytes.fromhex(evidence_fingerprint)
    except ValueError:
        raise ValueError("invalid Claude runtime evidence fingerprint") from None
    if len(evidence_digest) != 32 or len(source_digest) != 32:
        raise ValueError("invalid Claude runtime digest input")
    return hashlib.sha256(RESOURCE_DOMAIN + evidence_digest + source_digest).hexdigest()
