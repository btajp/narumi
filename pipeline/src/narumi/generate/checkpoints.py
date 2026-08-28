"""Durable, prompt-keyed progress for connected minutes generation.

Only completed replies are reused. A process crash or a reply with an unknown outcome leaves
a receipt that prevents an automatic resend; a changed ``cache_epoch`` creates a new attempt.
Prompts and authentication material are not stored in these receipts.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle
from narumi.bundle.hashing import sha256_params
from narumi.errors import CancelledError, EngineUnavailableError, NarumiError
from narumi.generate.bounded import MinutesLimits
from narumi.llm.base import LLMProvider

OUTCOME_UNKNOWN = "provider_generation_outcome_unknown"
LEGACY_OUTCOME_UNKNOWN = "codex_generation_outcome_unknown"
_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    }
)


def check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError("Minutes generation was cancelled")


class MinutesCheckpoints:
    """An LLM-provider wrapper; callers already hold the meeting's write lock."""

    def __init__(
        self,
        bundle: Bundle,
        provider: LLMProvider,
        *,
        inputs: dict[str, str],
        params: dict[str, Any],
        limits: MinutesLimits,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.bundle = bundle
        self.provider = provider
        self.name = provider.name
        self.profile = provider.profile
        self.should_cancel = should_cancel
        self.limits = limits
        identity = sha256_params({"inputs": inputs, "params": params})
        self.path = f"minutes/checkpoints/{identity}.json"
        self.document: dict[str, Any] = {"version": 1, "attempts": 0, "entries": {}}
        if bundle.abspath(self.path).exists():
            try:
                saved = bundle.read_json(self.path)
                if (
                    not isinstance(saved, dict)
                    or set(saved) != {"version", "attempts", "entries"}
                    or saved["version"] != 1
                    or not isinstance(saved["entries"], dict)
                    or type(saved["attempts"]) is not int
                    or saved["attempts"] < 0
                ):
                    raise ValueError
                self.document = saved
            except (OSError, ValueError, TypeError):
                raise EngineUnavailableError(
                    "The minutes checkpoint could not be verified; start a new attempt"
                ) from None

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        check_cancelled(self.should_cancel)
        if images:
            raise EngineUnavailableError("Minutes checkpoints accept text generation only")
        if len(prompt) + len(system or "") > self.limits.input_chars:
            raise EngineUnavailableError(
                "The minutes prompt exceeds the adapter input budget",
                details={"reason": "minutes_generation_limit"},
            )
        key = sha256_params({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        entries = self.document["entries"]
        saved = entries.get(key)
        if saved is not None:
            if not isinstance(saved, dict) or saved.get("state") != "succeeded":
                raise _unknown_result()
            answer = saved.get("answer")
            if not isinstance(answer, str) or saved.get("sha256") != _digest(answer):
                raise EngineUnavailableError(
                    "The minutes checkpoint response could not be verified; start a new attempt"
                )
            return answer
        if self.document["attempts"] >= self.limits.max_requests:
            raise EngineUnavailableError(
                "This minutes attempt has reached its request budget",
                details={"reason": "minutes_generation_limit"},
            )
        self.document["attempts"] += 1
        entries[key] = {"state": "pending"}
        try:
            self._save()  # receipt is durable before any possible outgoing request
        except Exception:
            raise EngineUnavailableError(
                "The minutes request checkpoint could not be saved; no request was sent",
                details={"reason": "minutes_checkpoint_unavailable"},
            ) from None
        try:
            answer = self.provider.complete(prompt, system=system, max_tokens=max_tokens)
        except BaseException as error:
            if _outcome_unknown(error):
                entries[key] = {"state": "unknown"}
            else:
                del entries[key]
            try:
                self._save()
            except Exception:
                raise _unknown_result() from None
            raise
        try:
            if not isinstance(answer, str) or not answer.strip():
                raise _unknown_result()
            entries[key] = {
                "state": "succeeded",
                "answer": answer,
                "sha256": _digest(answer),
                **_completion_metadata(self.provider),
            }
            self._save()
        except Exception:
            raise _unknown_result() from None
        check_cancelled(self.should_cancel)
        return answer

    def _save(self) -> None:
        path = self.bundle.write_json(self.path, self.document)
        _sync_checkpoint(path)


def _sync_checkpoint(path: Path) -> None:
    """Flush both the file's contents and its atomic replacement before dispatch."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    # The checkpoints directory can have been created by this very first request. Flush its
    # entry in minutes/ as well, or a crash could discard the directory and its receipt.
    for parent in (path.parent, path.parent.parent):
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _outcome_unknown(error: BaseException) -> bool:
    if isinstance(error, NarumiError):
        return bool(error.details.get("outcome_unknown")) or (
            error.details.get("reason") in {OUTCOME_UNKNOWN, LEGACY_OUTCOME_UNKNOWN}
        )
    # An unexpected exception after dispatch cannot prove that the request was not sent.
    return True


def _unknown_result() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The previous generation outcome is unknown; explicitly start a new attempt to resend",
        details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True},
    )


def _completion_metadata(provider: LLMProvider) -> dict[str, Any]:
    """Store only the adapter's non-secret completion facts, never request parameters.

    A missing usage object remains unknown; it does not imply zero usage. Metadata is
    deliberately outside the input fingerprint and old Codex receipts need no migration.
    """
    metadata = getattr(provider, "last_completion_metadata", None)
    if metadata is None:
        return {}
    if not isinstance(metadata, dict) or set(metadata) != {"returned_model", "usage"}:
        raise _unknown_result()
    model, usage = metadata["returned_model"], metadata["usage"]
    if not isinstance(model, str) or not model or len(model) > 256 or not model.isprintable():
        raise _unknown_result()
    if usage is not None and (
        not isinstance(usage, dict)
        or set(usage) - _USAGE_FIELDS
        or any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in usage.values())
    ):
        raise _unknown_result()
    return {"returned_model": model, "usage": None if usage is None else dict(usage)}
