"""Structured errors shared by pipeline and server.

Error codes mirror ``contracts/defs/common.json#/$defs/error_code``. Keep the two in sync:
``pipeline/tests/contracts`` asserts that every code here exists in the contract.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    SCOPE_DENIED = "scope_denied"
    CONTRACT_MISMATCH = "contract_mismatch"
    BUSY = "busy"
    CANCELLED = "cancelled"
    INVALID_ARGUMENT = "invalid_argument"
    POLICY_VIOLATION = "policy_violation"
    RECORDER_UNAVAILABLE = "recorder_unavailable"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    CONFIGURATION_CONFLICT = "configuration_conflict"
    AUTHENTICATION_REQUIRED = "authentication_required"
    MODEL_UNAVAILABLE = "model_unavailable"
    INTERNAL = "internal"


class NarumiError(Exception):
    """Base error carrying a structured code so servers can return it verbatim."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class NotFoundError(NarumiError):
    code = ErrorCode.NOT_FOUND


class ScopeDeniedError(NarumiError):
    code = ErrorCode.SCOPE_DENIED


class ContractMismatchError(NarumiError):
    code = ErrorCode.CONTRACT_MISMATCH


class BusyError(NarumiError):
    code = ErrorCode.BUSY


class CancelledError(NarumiError):
    """A job was cancelled cooperatively (``cancel_job``); not a failure."""

    code = ErrorCode.CANCELLED


class InvalidArgumentError(NarumiError):
    code = ErrorCode.INVALID_ARGUMENT


class PolicyViolationError(NarumiError):
    code = ErrorCode.POLICY_VIOLATION


class RecorderUnavailableError(NarumiError):
    code = ErrorCode.RECORDER_UNAVAILABLE


class EngineUnavailableError(NarumiError):
    code = ErrorCode.ENGINE_UNAVAILABLE


class ConfigurationConflictError(NarumiError):
    code = ErrorCode.CONFIGURATION_CONFLICT


class AuthenticationRequiredError(NarumiError):
    code = ErrorCode.AUTHENTICATION_REQUIRED


class ModelUnavailableError(NarumiError):
    code = ErrorCode.MODEL_UNAVAILABLE
