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
_GUARD_BLOCKING_STATES = frozenset({"pending", "unknown"})
_GUARD_RESOLVED_STATES = frozenset({"succeeded", "retryable"})
_GUARD_ENTRY_STATES = _GUARD_BLOCKING_STATES | _GUARD_RESOLVED_STATES
_GUARD_PATH = "minutes/checkpoints/attempts/ledger.json"
_GUARD_VERSION = 4
_ATTEMPT_SCOPE = "meeting-minutes"
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
        # Successful chunks remain keyed by the complete provenance above so a runtime or
        # catalog update can produce a legitimate new version. Unknown outcomes additionally
        # use a stable attempt identity: observational provenance must never create a new path
        # that silently bypasses the explicit cache_epoch retry boundary.
        # The bundle-local ledger already scopes this identity to one meeting. Artifact hashes
        # are observational provenance: changing slides, a brief, or merged output must create
        # a new successful-cache path without authorizing another possibly paid request at the
        # same retry epoch.
        attempt_identity = sha256_params(
            {"scope": _ATTEMPT_SCOPE, "params": _stable_attempt_params(params)}
        )
        self.attempt_identity = attempt_identity
        self.attempt_epoch = _attempt_epoch(params)
        self.guard_path = _GUARD_PATH
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
        self.guard_document: dict[str, Any] = {"version": _GUARD_VERSION, "entries": {}}
        if bundle.abspath(self.guard_path).exists():
            try:
                saved_guard = bundle.read_json(self.guard_path)
                entries = saved_guard.get("entries") if isinstance(saved_guard, dict) else None
                if (
                    not isinstance(saved_guard, dict)
                    or set(saved_guard) != {"version", "entries"}
                    or saved_guard["version"] != _GUARD_VERSION
                    or not isinstance(entries, dict)
                    or any(
                        not isinstance(key, str)
                        or len(key) != 64
                        or any(character not in "0123456789abcdef" for character in key)
                        or not isinstance(entry, dict)
                        or set(entry)
                        != {
                            "state",
                            "attempt_identity",
                            "attempt_epoch",
                            "checkpoint_sha256",
                            "prompt_sha256",
                        }
                        or entry["state"] not in _GUARD_ENTRY_STATES
                        or not _is_sha256(entry["attempt_identity"])
                        or type(entry["attempt_epoch"]) is not int
                        or entry["attempt_epoch"] < 0
                        or not _is_sha256(entry["checkpoint_sha256"])
                        or not _is_sha256(entry["prompt_sha256"])
                        or key
                        != _guard_entry_key(
                            entry["attempt_identity"],
                            entry["attempt_epoch"],
                            entry["prompt_sha256"],
                        )
                        for key, entry in entries.items()
                    )
                ):
                    raise ValueError
                self.guard_document = saved_guard
            except (OSError, ValueError, TypeError):
                raise EngineUnavailableError(
                    "The minutes retry guard could not be verified; start a new attempt"
                ) from None
        self._verify_guard_inventory()

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
            self._resolve_guard(key, resolution="succeeded")
            return answer
        if any(
            entry["attempt_identity"] == self.attempt_identity
            and entry["attempt_epoch"] >= self.attempt_epoch
            and entry["state"] in _GUARD_BLOCKING_STATES
            for entry in self.guard_document["entries"].values()
        ):
            raise _unknown_result()
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
        guard_key = self._guard_key(key)
        self.guard_document["entries"][guard_key] = {
            "state": "pending",
            "attempt_identity": self.attempt_identity,
            "attempt_epoch": self.attempt_epoch,
            "checkpoint_sha256": Path(self.path).stem,
            "prompt_sha256": key,
        }
        try:
            self._save_guard()  # stable receipt is also durable before provider dispatch
        except Exception:
            raise EngineUnavailableError(
                "The minutes retry guard could not be saved; no request was sent",
                details={"reason": "minutes_checkpoint_unavailable"},
            ) from None
        try:
            answer = self.provider.complete(prompt, system=system, max_tokens=max_tokens)
        except BaseException as error:
            unknown = _outcome_unknown(error)
            if unknown:
                entries[key] = {"state": "unknown"}
                self.guard_document["entries"][guard_key]["state"] = "unknown"
            else:
                del entries[key]
            try:
                self._save()
                if unknown:
                    self._save_guard()
                else:
                    # Keep the durable guard until the full checkpoint has first recorded that
                    # this known failure is retryable.
                    self._resolve_guard(key, resolution="retryable")
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
            self._resolve_guard(key, resolution="succeeded")
        except Exception:
            raise _unknown_result() from None
        check_cancelled(self.should_cancel)
        return answer

    def _save(self) -> None:
        path = self.bundle.write_json(self.path, self.document)
        _sync_checkpoint(path)

    def _save_guard(self) -> None:
        path = self.bundle.write_json(self.guard_path, self.guard_document)
        _sync_checkpoint(path)

    def _resolve_guard(self, key: str, *, resolution: str = "succeeded") -> None:
        if resolution not in _GUARD_RESOLVED_STATES:
            raise ValueError("unsupported minutes guard resolution")
        guard_key = self._guard_key(key)
        entry = self.guard_document["entries"].get(guard_key)
        if entry is None:
            return
        if entry["checkpoint_sha256"] != Path(self.path).stem:
            # Another full-provenance attempt (for example a newer runtime) owns this pending
            # receipt. Reusing an older successful cache must not erase that uncertain send.
            return
        if entry["state"] == resolution:
            return
        # A succeeded full-provenance checkpoint can coexist with an older/newer unknown
        # attempt for the same prompt. Only the pending receipt for this exact epoch is safe
        # to resolve; an unknown receipt is never superseded by cache reuse.
        if entry["state"] != "pending":
            raise _unknown_result()
        previous = entry.copy()
        entry["state"] = resolution
        try:
            self._save_guard()
        except BaseException:
            # Atomic replacement can expose either the old blocking entry or the new resolution
            # after an fsync failure. Both remain fail-closed because a resolution is accepted
            # only when the referenced full checkpoint proves it below. Restore memory so reuse
            # of this object is conservative too.
            entry.clear()
            entry.update(previous)
            raise

    def _guard_key(self, prompt_key: str) -> str:
        return _guard_entry_key(self.attempt_identity, self.attempt_epoch, prompt_key)

    def _verify_guard_inventory(self) -> None:
        """Reject legacy/unpaired unknown receipts whose retry epoch cannot be proven."""
        attempts = self.bundle.abspath("minutes/checkpoints/attempts")
        if attempts.exists():
            try:
                names = {path.name for path in attempts.iterdir()}
            except OSError:
                raise EngineUnavailableError(
                    "The minutes retry guard could not be verified; start a new attempt"
                ) from None
            if names - {Path(_GUARD_PATH).name}:
                raise EngineUnavailableError(
                    "The minutes retry guard could not be verified; start a new attempt"
                )

        referenced = {
            entry["checkpoint_sha256"] for entry in self.guard_document["entries"].values()
        }
        checkpoint_dir = self.bundle.abspath("minutes/checkpoints")
        if not checkpoint_dir.exists():
            return
        try:
            paths = list(checkpoint_dir.glob("*.json"))
            for path in paths:
                saved = self.bundle.read_json(f"minutes/checkpoints/{path.name}")
                entries = saved.get("entries") if isinstance(saved, dict) else None
                if (
                    not _is_sha256(path.stem)
                    or not isinstance(saved, dict)
                    or set(saved) != {"version", "attempts", "entries"}
                    or saved["version"] != 1
                    or type(saved["attempts"]) is not int
                    or saved["attempts"] < 0
                    or not isinstance(entries, dict)
                    or any(
                        not _is_sha256(key)
                        or not isinstance(entry, dict)
                        or entry.get("state") not in {"pending", "unknown", "succeeded"}
                        for key, entry in entries.items()
                    )
                ):
                    raise ValueError
                if (
                    any(entry["state"] in _GUARD_BLOCKING_STATES for entry in entries.values())
                    and path.stem not in referenced
                ):
                    raise ValueError
            if any(
                not self.bundle.abspath(f"minutes/checkpoints/{checkpoint}.json").is_file()
                for checkpoint in referenced
            ):
                raise ValueError
            for guard in self.guard_document["entries"].values():
                if guard["state"] not in _GUARD_RESOLVED_STATES:
                    continue
                saved = self.bundle.read_json(
                    f"minutes/checkpoints/{guard['checkpoint_sha256']}.json"
                )
                checkpoint_entry = saved["entries"].get(guard["prompt_sha256"])
                if guard["state"] == "retryable":
                    if checkpoint_entry is not None:
                        raise ValueError
                    continue
                answer = (
                    checkpoint_entry.get("answer") if isinstance(checkpoint_entry, dict) else None
                )
                if (
                    not isinstance(checkpoint_entry, dict)
                    or checkpoint_entry.get("state") != "succeeded"
                    or not isinstance(answer, str)
                    or checkpoint_entry.get("sha256") != _digest(answer)
                ):
                    raise ValueError
        except (OSError, ValueError, TypeError):
            raise EngineUnavailableError(
                "The minutes retry guard could not be verified; start a new attempt"
            ) from None


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


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _attempt_epoch(params: dict[str, Any]) -> int:
    selection = params.get("minutes_model")
    if not isinstance(selection, dict):
        return 0
    epoch = selection.get("cache_epoch")
    if type(epoch) is not int or epoch < 0:
        raise EngineUnavailableError(
            "The minutes retry epoch could not be verified; no request was sent"
        )
    return epoch


def _guard_entry_key(attempt_identity: str, attempt_epoch: int, prompt_sha256: str) -> str:
    return sha256_params(
        {
            "attempt_identity": attempt_identity,
            "attempt_epoch": attempt_epoch,
            "prompt_sha256": prompt_sha256,
        }
    )


def _stable_attempt_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return the user-controlled retry identity, excluding observational provenance.

    ``connection_revision``, prompt/language revisions, runtime/catalog fingerprints and
    model-verification timestamps can change without the user authorizing another paid request.
    Resolver-derived effective defaults and output limits are observations too; the saved model
    selection is the authority. Successful checkpoints still use the complete ``params``
    document; this projection exists only for unknown-outcome guards. The retry epoch is stored
    in the ledger state rather than the identity so a decrease or reuse can be compared with
    every earlier uncertain attempt.
    """
    selection = params.get("minutes_model")
    if not isinstance(selection, dict):
        # Non-connected fixture/legacy providers have no separate retry controls. Retain their
        # existing full-parameter boundary instead of guessing at an incomplete identity.
        return params
    return {
        "minutes_model": {
            "provider": selection.get("provider"),
            "connection_id": selection.get("connection_id"),
            "model_id": selection.get("model_id"),
            "parameters": selection.get("parameters"),
        },
    }


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
