"""Durable audio attempts: verified successes are reused, unknowns require one proof."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from narumi.bundle import Bundle
from narumi.bundle.hashing import sha256_bytes
from narumi.errors import ConfigurationConflictError, EngineUnavailableError, NarumiError
from narumi.transcribe._checkpoint_format import (
    UNKNOWN_STATES,
    VERSION,
    result_name,
    validate_document,
    validate_stored_plan,
)
from narumi.transcribe._storage import (
    MAX_JSON_BYTES,
    canonical_bytes,
    check_cancelled,
    cleanup_temporaries,
    locked_ledger,
    read_bytes,
    require_execution_lease,
    storage_directory,
    storage_error,
    strict_json,
    transcription_execution_lock,
    write_bytes,
)
from narumi.transcribe.chunks import TranscriptionChunk, TranscriptionPlan
from narumi.transcription_selection import TranscriptionRetry

__all__ = ["TranscriptionAttempt", "TranscriptionCheckpoints", "transcription_execution_lock"]
OUTCOME_UNKNOWN = "transcription_outcome_unknown"


@dataclass(frozen=True)
class TranscriptionAttempt:
    chunk_fingerprint: str
    attempt_id: str
    epoch: int
    was_unknown: bool


def _conflict() -> ConfigurationConflictError:
    return ConfigurationConflictError(
        "The transcription retry confirmation no longer matches this input and attempt",
        details={"stage": "transcribe", "reason": "transcription_retry_conflict"},
    )


class TranscriptionCheckpoints:
    """Use inside ``transcription_execution_lock`` through final transcript publication.

    Every mutation rereads the ledger and compares the attempt identity. A result file is
    flushed before the successful ledger entry; a crash at either earlier point leaves the
    already-durable pending receipt blocked. The ledger is shared across input plans so an
    unrelated track change cannot bypass the outcome of an identical audio chunk.
    """

    def __init__(
        self,
        bundle: Bundle,
        plan: TranscriptionPlan,
        *,
        cache_epoch: int,
        retry: TranscriptionRetry | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        require_execution_lease(bundle)
        plan.validate()
        if type(cache_epoch) is not int or cache_epoch < 0:
            raise _conflict()
        if retry is not None and not isinstance(retry, TranscriptionRetry):
            raise _conflict()
        self.bundle, self.plan = bundle, plan
        self.cache_epoch = cache_epoch
        self.retry = retry.model_copy(deep=True) if retry is not None else None
        self.should_cancel = should_cancel
        self._preflight_done = False
        self._proof_consumed = False
        self._completed = 0
        self._chunks = {chunk.fingerprint: chunk for chunk in plan.chunks}
        self._initialize()

    def _initialize(self) -> None:
        with locked_ledger(self.bundle) as directory:
            saved = read_bytes(directory, "ledger.json")
            if saved is None:
                # Deterministic WAV chunks can exist before a first attempt. A plan or
                # result, however, proves that this is not a fresh checkpoint namespace.
                for name in ("plans", "results"):
                    with storage_directory(self.bundle, name) as nested:
                        cleanup_temporaries(nested)
                        if os.listdir(nested):
                            raise storage_error()
                document = {"version": VERSION, "plans": {}, "entries": {}}
            else:
                document = validate_document(strict_json(saved))
                self._verify_existing_plans(document)
            fingerprint = self.plan.input_fingerprint
            with storage_directory(self.bundle, "plans") as plans:
                stored = read_bytes(plans, f"{fingerprint}.json")
                if fingerprint in document["plans"]:
                    self._verify_plan(document, stored)
                    return
                if stored is not None:
                    raise storage_error()
                for chunk in self.plan.chunks:
                    document["entries"].setdefault(chunk.fingerprint, {"state": "unattempted"})
                document["plans"][fingerprint] = [chunk.fingerprint for chunk in self.plan.chunks]
                validate_document(document)
                write_bytes(
                    plans,
                    f"{fingerprint}.json",
                    canonical_bytes(self.plan.as_payload()),
                    immutable=True,
                )
                self._save(directory, document)

    def _verify_existing_plans(self, document: dict[str, Any]) -> None:
        with storage_directory(self.bundle, "plans") as plans:
            cleanup_temporaries(plans)
            expected = {f"{fingerprint}.json" for fingerprint in document["plans"]}
            if set(os.listdir(plans)) != expected:
                raise storage_error()
            for fingerprint, chunk_ids in document["plans"].items():
                check_cancelled(self.should_cancel)
                stored = read_bytes(plans, f"{fingerprint}.json")
                if stored is None:
                    raise storage_error()
                payload = strict_json(stored)
                actual = validate_stored_plan(payload, self.bundle.path.resolve())
                if payload["input_fingerprint"] != fingerprint or actual != chunk_ids:
                    raise storage_error()

    def _verify_plan(self, document: dict[str, Any], stored: bytes | None = None) -> None:
        if document["plans"].get(self.plan.input_fingerprint) != [
            chunk.fingerprint for chunk in self.plan.chunks
        ]:
            raise storage_error()
        if stored is None:
            with storage_directory(self.bundle, "plans") as plans:
                stored = read_bytes(plans, f"{self.plan.input_fingerprint}.json")
        if stored is None or canonical_bytes(strict_json(stored)) != canonical_bytes(
            self.plan.as_payload()
        ):
            raise storage_error()

    def _read(self, directory: int) -> dict[str, Any]:
        saved = read_bytes(directory, "ledger.json")
        if saved is None:
            raise storage_error()
        document = validate_document(strict_json(saved))
        self._verify_plan(document)
        return document

    @staticmethod
    def _save(directory: int, document: dict[str, Any]) -> None:
        encoded = canonical_bytes(validate_document(document))
        if len(encoded) > MAX_JSON_BYTES:
            raise storage_error()
        write_bytes(directory, "ledger.json", encoded)

    def _chunk(self, chunk: TranscriptionChunk) -> TranscriptionChunk:
        expected = self._chunks.get(chunk.fingerprint)
        if expected is None or expected != chunk:
            raise _conflict()
        return expected

    def _saved_result(self, chunk: TranscriptionChunk, entry: dict[str, Any]) -> dict[str, Any]:
        with storage_directory(self.bundle, "results") as results:
            encoded = read_bytes(results, result_name(chunk.fingerprint, entry))
        if encoded is None or sha256_bytes(encoded) != entry["result_sha256"]:
            raise storage_error()
        payload = strict_json(encoded)
        self._validate_result(chunk, payload)
        return payload

    def _validate_result(self, chunk: TranscriptionChunk, payload: Any) -> None:
        from narumi.providers.audio_response import parse_saved_result

        try:
            parse_saved_result(
                payload, model_id=self.plan.params["model_id"], chunk_duration=chunk.duration_sec
            )
        except Exception:
            raise storage_error() from None

    def _first_unknown(self, document: dict[str, Any]) -> TranscriptionChunk | None:
        return next(
            (
                chunk
                for chunk in self.plan.chunks
                if document["entries"][chunk.fingerprint]["state"] in UNKNOWN_STATES
            ),
            None,
        )

    def _verify_retry(self, document: dict[str, Any]) -> None:
        first = self._first_unknown(document)
        if self.retry is None or first is None:
            raise _conflict()
        entry = document["entries"][first.fingerprint]
        if (
            self.retry.input_fingerprint != self.plan.input_fingerprint
            or self.retry.chunk_fingerprint != first.fingerprint
            or self.retry.blocked_epoch != entry["epoch"]
            or self.cache_epoch <= entry["epoch"]
        ):
            raise _conflict()

    def preflight(self) -> None:
        """Validate every prior success and the retry proof before the first audio send."""
        self._preflight_done = False
        check_cancelled(self.should_cancel)
        with locked_ledger(self.bundle) as directory:
            document = self._read(directory)
            self._verify_existing_plans(document)
            completed = 0
            for chunk in self.plan.chunks:
                check_cancelled(self.should_cancel)
                entry = document["entries"][chunk.fingerprint]
                if entry["state"] == "succeeded":
                    self._saved_result(chunk, entry)
                    completed += 1
            self._completed = completed
            first = self._first_unknown(document)
            if self.retry is not None:
                if self._proof_consumed:
                    raise _conflict()
                self._verify_retry(document)
            elif first is not None:
                raise self._unknown(first, document["entries"][first.fingerprint]["epoch"])
            self._preflight_done = True

    def _require_preflight(self) -> None:
        require_execution_lease(self.bundle)
        if not self._preflight_done:
            raise _conflict()

    def get_success(self, chunk: TranscriptionChunk) -> dict[str, Any] | None:
        self._require_preflight()
        self._chunk(chunk)
        check_cancelled(self.should_cancel)
        with locked_ledger(self.bundle) as directory:
            entry = self._read(directory)["entries"][chunk.fingerprint]
            return self._saved_result(chunk, entry) if entry["state"] == "succeeded" else None

    def begin_attempt(self, chunk: TranscriptionChunk) -> TranscriptionAttempt:
        """Consume at most one matching unknown proof with the durable pending receipt."""
        self._require_preflight()
        self._chunk(chunk)
        check_cancelled(self.should_cancel)
        with locked_ledger(self.bundle) as directory:
            document = self._read(directory)
            if self.retry is not None and not self._proof_consumed:
                self._verify_retry(document)
            for previous in self.plan.chunks[: chunk.index]:
                prior = document["entries"][previous.fingerprint]
                if prior["state"] in UNKNOWN_STATES:
                    raise self._unknown(previous, prior["epoch"])
                if prior["state"] != "succeeded":
                    raise _conflict()
            entry = document["entries"][chunk.fingerprint]
            was_unknown = entry["state"] in UNKNOWN_STATES
            if entry["state"] == "succeeded":
                raise _conflict()
            if was_unknown and (
                self.retry is None
                or self._proof_consumed
                or self.retry.chunk_fingerprint != chunk.fingerprint
            ):
                raise self._unknown(chunk, entry["epoch"])
            attempt = TranscriptionAttempt(
                chunk.fingerprint, uuid.uuid4().hex, self.cache_epoch, was_unknown
            )
            document["entries"][chunk.fingerprint] = {
                "state": "pending",
                "attempt_id": attempt.attempt_id,
                "epoch": attempt.epoch,
                "was_unknown": was_unknown,
            }
            try:
                self._save(directory, document)
            except Exception:
                raise storage_error() from None
            if was_unknown:
                self._proof_consumed = True
            return attempt

    def _pending(self, document: dict[str, Any], attempt: TranscriptionAttempt) -> dict[str, Any]:
        entry = document["entries"].get(attempt.chunk_fingerprint)
        if (
            entry is None
            or entry["state"] != "pending"
            or entry["attempt_id"] != attempt.attempt_id
            or entry["epoch"] != attempt.epoch
            or entry["was_unknown"] != attempt.was_unknown
        ):
            raise _conflict()
        return entry

    def succeed(self, attempt: TranscriptionAttempt, result: dict[str, Any]) -> None:
        """Persist the complete result before checking cancellation in the caller."""
        self._require_preflight()
        chunk = self._chunks.get(attempt.chunk_fingerprint)
        if chunk is None:
            raise _conflict()
        try:
            encoded = canonical_bytes(result)
            if len(encoded) > MAX_JSON_BYTES:
                raise storage_error()
            payload = strict_json(encoded)
            self._validate_result(chunk, payload)
            with locked_ledger(self.bundle) as directory:
                document = self._read(directory)
                entry = self._pending(document, attempt)
                with storage_directory(self.bundle, "results") as results:
                    write_bytes(
                        results, result_name(chunk.fingerprint, entry), encoded, immutable=True
                    )
                document["entries"][chunk.fingerprint] = {
                    **entry,
                    "state": "succeeded",
                    "result_sha256": sha256_bytes(encoded),
                }
                self._save(directory, document)
            self._completed += 1
        except ConfigurationConflictError:
            raise
        except Exception:
            # The pending receipt was persisted before dispatch. If result/ledger saving
            # fails, never remove that receipt or turn this into a safe cache miss.
            raise self._unknown(chunk, attempt.epoch) from None

    def fail(self, attempt: TranscriptionAttempt, error: BaseException) -> None:
        self._require_preflight()
        chunk = self._chunks.get(attempt.chunk_fingerprint)
        if chunk is None:
            raise _conflict()
        unknown = attempt.was_unknown or _outcome_unknown(error)
        try:
            with locked_ledger(self.bundle) as directory:
                document = self._read(directory)
                entry = self._pending(document, attempt)
                document["entries"][chunk.fingerprint] = {
                    **entry,
                    "state": "unknown" if unknown else "known_failed",
                }
                self._save(directory, document)
        except ConfigurationConflictError:
            raise
        except Exception:
            raise self._unknown(chunk, attempt.epoch) from None
        if unknown:
            raise self._unknown(chunk, attempt.epoch) from None

    def _unknown(self, chunk: TranscriptionChunk, epoch: int) -> EngineUnavailableError:
        return EngineUnavailableError(
            "The audio transcription outcome is unknown; confirm this chunk before resending",
            details={
                "stage": "transcribe",
                "reason": OUTCOME_UNKNOWN,
                "outcome_unknown": True,
                "input_fingerprint": self.plan.input_fingerprint,
                "chunk_fingerprint": chunk.fingerprint,
                "blocked_epoch": epoch,
                "track": chunk.track,
                "chunk_index": chunk.index,
                "chunk_count": len(self.plan.chunks),
                "completed_chunks": self._completed,
                "start_sample": chunk.start_sample,
                "end_sample": chunk.end_sample,
                "sample_rate": chunk.sample_rate,
            },
        )


def _outcome_unknown(error: BaseException) -> bool:
    if isinstance(error, NarumiError):
        if not isinstance(error.details, dict):
            return True
        reason = error.details.get("reason")
        if reason is not None and not isinstance(reason, str):
            return True
        return bool(error.details.get("outcome_unknown")) or reason in {
            OUTCOME_UNKNOWN,
            "provider_transcription_outcome_unknown",
        }
    return True
