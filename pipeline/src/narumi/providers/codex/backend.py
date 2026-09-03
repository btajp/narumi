"""Connection-scoped Codex runtime, official login and text generation backend."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
)
from narumi.providers.codex import _models, _policy
from narumi.providers.codex._rpc import PROCESS_CLEANUP_REASON, StdioRPC, unavailable
from narumi.providers.codex._runtime import CodexRuntime
from narumi.providers.codex._session import CodexSession, clear_credentials, connection_directory

AUTH_TIMEOUT = 600.0
_LOGOUT_WAIT = 5.0
_AUTH_GENERATION_MISSING = object()
_AUTH_GENERATION_UNCHECKED = object()


@dataclass(frozen=True)
class _AuthGeneration:
    operation_id: str
    phase: str


@dataclass
class _Operation:
    connection_id: str
    kind: str
    closed: threading.Event
    callback: Callable[[], bool]
    poison: Callable[[_Operation], None]
    operation_id: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    cleanup_unverified: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    rpc: StdioRPC | None = None

    def should_cancel(self) -> bool:
        return self._cancel_latched() or self.callback()

    def _cancel_latched(self) -> bool:
        return self.closed.is_set() or self.cancelled.is_set() or self.cleanup_unverified.is_set()

    def mark_cleanup_unverified(self) -> None:
        self.cleanup_unverified.set()
        self.poison(self)

    def attach(self, rpc: StdioRPC) -> None:
        with self.lock:
            self.rpc = rpc
        if self.should_cancel():
            rpc.cancel()

    def detach(self, rpc: StdioRPC) -> None:
        with self.lock:
            if self.rpc is rpc:
                self.rpc = None

    def cancel(self) -> None:
        with self.lock:
            self.cancelled.set()
            rpc = self.rpc
        if rpc is not None:
            rpc.cancel()

    def install_credentials(self, install: Callable[[], None]) -> bool:
        """Linearize a credential commit against cancellation for this operation."""
        if self.should_cancel():
            return False
        with self.lock:
            if self._cancel_latched():
                return False
            install()
            active = not self._cancel_latched()
        return active and not self.should_cancel()


class CodexBackend:
    """No process, credential lookup or file mutation during construction/listing."""

    def __init__(self, root: Path) -> None:
        self.runtime = CodexRuntime(root)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._operations: dict[str, _Operation] = {}
        self._poisoned_connections: set[str] = set()
        self._auth_generations: dict[str, _AuthGeneration] = {}

    def resource(self) -> dict[str, Any]:
        return self.runtime.resource()

    def prepare(self, resource: dict[str, Any], progress: Any) -> None:
        if self._closed.is_set():
            raise unavailable("codex_backend_closed")
        try:
            _policy.host_preflight()
            self.runtime.prepare(resource, progress)
        except NarumiError:
            raise
        except Exception:
            raise unavailable("codex_runtime_not_secure") from None

    @contextmanager
    def _operation(
        self,
        connection_id: str,
        kind: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        operation_id: str | None = None,
        expected_auth: str | object = _AUTH_GENERATION_UNCHECKED,
        allowed_auth_phases: frozenset[str] = frozenset(
            {"registered", "cleanup_required", "prepared", "settled"}
        ),
        auth_success_phase: str | None = None,
        auth_failure_phase: str | None = None,
        auth_final_phase: str | None = None,
        invalidate_auth_on_success: bool = False,
    ) -> Iterator[_Operation]:
        connection_directory(self.runtime.root, connection_id)
        with self._lock:
            if connection_id in self._poisoned_connections and kind not in {
                "cancel_cleanup",
                "logout",
            }:
                raise unavailable(PROCESS_CLEANUP_REASON)
            if self._closed.is_set():
                raise unavailable("codex_backend_closed")
            if connection_id in self._operations:
                raise BusyError("This Codex connection already has an active operation")
            generation = self._auth_generations.get(connection_id, _AUTH_GENERATION_MISSING)
            if expected_auth is not _AUTH_GENERATION_UNCHECKED:
                if expected_auth is _AUTH_GENERATION_MISSING:
                    matches_generation = generation is _AUTH_GENERATION_MISSING
                else:
                    matches_generation = (
                        isinstance(generation, _AuthGeneration)
                        and generation.operation_id == expected_auth
                        and generation.phase in allowed_auth_phases
                    )
                if not matches_generation:
                    raise CancelledError("Codex authentication operation is no longer current")
            operation = _Operation(
                connection_id,
                kind,
                self._closed,
                cancelled or (lambda: False),
                self._mark_poisoned,
                operation_id,
            )
            self._operations[connection_id] = operation
        primary_failure = False
        try:
            if operation.should_cancel():
                raise CancelledError("Codex operation was cancelled")
            yield operation
        except BaseException as error:
            primary_failure = True
            if isinstance(error, NarumiError):
                raise
            if isinstance(error, Exception):
                raise unavailable("codex_operation_failed") from None
            raise
        finally:
            cancel_failure: BaseException | None = None
            try:
                operation.cancel()
            except BaseException as error:
                cancel_failure = error
                self._mark_poisoned(operation)
            finally:
                try:
                    with self._lock:
                        if operation.cleanup_unverified.is_set():
                            self._poisoned_connections.add(connection_id)
                        generation = self._auth_generations.get(
                            connection_id, _AUTH_GENERATION_MISSING
                        )
                        generation_matches = (
                            isinstance(expected_auth, str)
                            and isinstance(generation, _AuthGeneration)
                            and generation.operation_id == expected_auth
                        )
                        if (
                            generation_matches
                            and auth_failure_phase is not None
                            and (primary_failure or cancel_failure is not None)
                        ):
                            self._auth_generations[connection_id] = _AuthGeneration(
                                expected_auth, auth_failure_phase
                            )
                        elif generation_matches and auth_final_phase is not None:
                            self._auth_generations[connection_id] = _AuthGeneration(
                                expected_auth, auth_final_phase
                            )
                        elif not primary_failure and cancel_failure is None:
                            if generation_matches and auth_success_phase is not None:
                                self._auth_generations[connection_id] = _AuthGeneration(
                                    expected_auth, auth_success_phase
                                )
                            if generation_matches and invalidate_auth_on_success:
                                del self._auth_generations[connection_id]
                        if self._operations.get(connection_id) is operation:
                            del self._operations[connection_id]
                finally:
                    operation.done.set()
            if cancel_failure is not None and not primary_failure:
                if isinstance(cancel_failure, Exception):
                    raise unavailable(PROCESS_CLEANUP_REASON) from None
                raise cancel_failure

    def authenticate(
        self,
        connection_id: str,
        *,
        on_authorization_code: Callable[[str, str], None],
        cancelled: Callable[[], bool],
        operation_id: str | None = None,
    ) -> None:
        operation_options: dict[str, Any] = {}
        if operation_id is not None:
            operation_options = {
                "expected_auth": operation_id,
                "allowed_auth_phases": frozenset({"prepared"}),
                "auth_final_phase": "settled",
            }
        else:
            operation_options = {"expected_auth": _AUTH_GENERATION_MISSING}
        with self._operation(
            connection_id,
            "auth",
            cancelled=cancelled,
            operation_id=operation_id,
            **operation_options,
        ) as operation:
            with CodexSession(self.runtime, connection_id, operation) as session:
                try:
                    result = session.call("account/login/start", {"type": "chatgptDeviceCode"})
                except EngineUnavailableError as error:
                    if error.details.get("reason") != "codex_rpc_failed":
                        raise
                    raise AuthenticationRequiredError(
                        "Codex device-code sign-in could not be started",
                        details={"reason": "device_code_login_unavailable"},
                    ) from None
                login_id = result.get("loginId")
                if (
                    result.get("type") != "chatgptDeviceCode"
                    or not isinstance(login_id, str)
                    or not 1 <= len(login_id) <= 256
                    or not login_id.isprintable()
                ):
                    raise unavailable("codex_login_response_rejected")
                url, user_code = _policy.device_authorization(
                    result.get("verificationUrl"), result.get("userCode")
                )
                # No slot lock is held while the UI receives these memory-only values.
                on_authorization_code(url, user_code)
                del url, user_code, result
                if session.rpc is None:
                    raise unavailable("codex_process_closed")
                deadline = time.monotonic() + AUTH_TIMEOUT
                completed = session.rpc.wait_for(
                    lambda message: (
                        message.get("method") == "account/login/completed"
                        and message.get("params", {}).get("loginId") == login_id
                    ),
                    timeout=AUTH_TIMEOUT,
                )
                params = completed.get("params", {})
                if params.get("success") is not True:
                    raise AuthenticationRequiredError(
                        "Codex ChatGPT sign-in did not complete",
                        details={"reason": "codex_login_failed"},
                    )
                # The official process installs its post-login configuration before
                # account/updated; login/completed alone is not a readiness signal.
                session.rpc.wait_for(
                    lambda message: (
                        message.get("method") == "account/updated"
                        and message.get("params", {}).get("authMode") == "chatgpt"
                    ),
                    timeout=max(0.01, deadline - time.monotonic()),
                )
                session.require_chatgpt()
                session.verify_configuration()

    def register_auth_generation(
        self,
        connection_id: str,
        *,
        operation_id: str,
        replace: bool,
        cleanup_required: bool,
    ) -> bool:
        """Register a store-validated generation without touching credentials."""
        connection_directory(self.runtime.root, connection_id)
        with self._lock:
            if self._closed.is_set():
                raise unavailable("codex_backend_closed")
            operation = self._operations.get(connection_id)
            current = self._auth_generations.get(connection_id)
            if operation is not None:
                if operation.kind in {"auth", "auth_prepare"} and (
                    operation.operation_id == operation_id
                ):
                    if current is None or current.operation_id != operation_id:
                        return False
                    if cleanup_required and current.phase == "registered":
                        self._auth_generations[connection_id] = _AuthGeneration(
                            operation_id, "cleanup_required"
                        )
                    return True
                if replace:
                    raise BusyError("This Codex connection already has an active operation")
                return False
            if current is not None and current.operation_id == operation_id:
                if cleanup_required and current.phase == "registered":
                    self._auth_generations[connection_id] = _AuthGeneration(
                        operation_id, "cleanup_required"
                    )
                return True
            if current is not None and not replace:
                return False
            phase = "cleanup_required" if cleanup_required else "registered"
            self._auth_generations[connection_id] = _AuthGeneration(operation_id, phase)
            return True

    def is_auth_generation_current(self, connection_id: str, *, operation_id: str) -> bool:
        """Check ownership without adopting an unknown or stale generation."""
        connection_directory(self.runtime.root, connection_id)
        with self._lock:
            current = self._auth_generations.get(connection_id)
            return current is not None and current.operation_id == operation_id

    def prepare_auth(self, connection_id: str, *, operation_id: str) -> bool:
        """Remove prior credentials only for the registered current generation."""
        with self._operation(
            connection_id,
            "auth_prepare",
            operation_id=operation_id,
            expected_auth=operation_id,
            allowed_auth_phases=frozenset({"registered"}),
            auth_success_phase="prepared",
            auth_failure_phase="cleanup_required",
        ):
            clear_credentials(self.runtime.root, connection_id)
        return True

    def list_models(self, connection_id: str) -> list[dict[str, Any]]:
        with self._operation(connection_id, "models") as operation:
            with CodexSession(self.runtime, connection_id, operation) as session:
                session.require_chatgpt()
                session.verify_configuration()
                return _models.fetch_models(session.call)

    def cancel_auth(self, connection_id: str, *, operation_id: str | None = None) -> bool:
        """Return true only after safe preservation or verified credential cleanup."""
        with self._lock:
            operation = self._operations.get(connection_id)
            generation = self._auth_generations.get(connection_id, _AUTH_GENERATION_MISSING)
            if operation is not None and operation.kind not in {"auth", "auth_prepare"}:
                return False
            if operation_id is not None:
                if (
                    not isinstance(generation, _AuthGeneration)
                    or generation.operation_id != operation_id
                    or (operation is not None and operation.operation_id != operation_id)
                ):
                    return False
                target_generation: str | object = operation_id
            else:
                if operation is not None and operation.operation_id is not None:
                    return False
                target_generation = (
                    _AUTH_GENERATION_MISSING
                    if generation is _AUTH_GENERATION_MISSING
                    else generation.operation_id
                )
        if operation is not None:
            # Wait until session teardown has passed the credential-persistence boundary.
            # A late cancel may race after the pre-copy cancellation check, so signalling
            # the worker alone is not proof that the persistent auth file is absent.
            operation.cancel()
            if not operation.done.wait(_LOGOUT_WAIT):
                raise BusyError("Codex authentication has not finished cancelling")
        # Reserve the connection while removing both persistent credentials and any
        # per-run copies. A concurrent replacement operation must win or lose this slot,
        # never start between the worker wait and cleanup verification.
        try:
            with self._operation(
                connection_id,
                "cancel_cleanup",
                expected_auth=target_generation,
                invalidate_auth_on_success=isinstance(target_generation, str),
            ):
                with self._lock:
                    current = self._auth_generations.get(connection_id, _AUTH_GENERATION_MISSING)
                    preserve_existing = (
                        isinstance(current, _AuthGeneration)
                        and current.operation_id == target_generation
                        and current.phase == "registered"
                    )
                if not preserve_existing:
                    clear_credentials(self.runtime.root, connection_id)
        except (BusyError, CancelledError):
            return False
        self._raise_if_poisoned(connection_id)
        return True

    def logout(self, connection_id: str) -> None:
        with self._lock:
            previous = self._operations.get(connection_id)
            generation = self._auth_generations.get(connection_id, _AUTH_GENERATION_MISSING)
        if previous is not None:
            if previous.kind != "auth":
                raise BusyError("This Codex connection is in use")
            previous.cancel()
            if not previous.done.wait(_LOGOUT_WAIT):
                raise BusyError("Codex authentication has not finished cancelling")
        expected_auth: str | object = (
            _AUTH_GENERATION_MISSING
            if generation is _AUTH_GENERATION_MISSING
            else generation.operation_id
        )
        with self._operation(
            connection_id,
            "logout",
            expected_auth=expected_auth,
            invalidate_auth_on_success=isinstance(expected_auth, str),
        ) as operation:
            if operation.should_cancel():
                raise CancelledError("Codex logout was cancelled")
            clear_credentials(self.runtime.root, connection_id)
        self._raise_if_poisoned(connection_id)

    def complete(
        self,
        connection_id: str,
        model_id: str,
        parameters: dict[str, Any],
        prompt: str,
        *,
        system: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        from narumi.providers.codex._generation import generate

        if max_tokens is not None:
            raise InvalidArgumentError("Codex App Server does not support a max_tokens parameter")
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > 2 * 1024 * 1024
            or (system is not None and not isinstance(system, str))
            or (system is not None and len(system.encode("utf-8")) > 128 * 1024)
            or not isinstance(parameters, dict)
            or set(parameters) - {"reasoning_effort"}
        ):
            raise InvalidArgumentError("Codex text generation arguments are invalid")
        with self._operation(connection_id, "generation", cancelled=should_cancel) as operation:
            with CodexSession(self.runtime, connection_id, operation) as metadata:
                metadata.require_chatgpt()
                metadata.verify_configuration()
                models = _models.fetch_models(metadata.call)
                model = _models.select_model(models, model_id, parameters)
            with CodexSession(self.runtime, connection_id, operation, model=model) as session:
                session.require_chatgpt()
                return generate(session, model, parameters, prompt, system=system)

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            operations = list(self._operations.values())
        if not operations:
            with self._lock:
                poisoned = bool(self._poisoned_connections)
            if poisoned:
                raise unavailable(PROCESS_CLEANUP_REASON)
            return
        failure: BaseException | None = None
        for operation in operations:
            try:
                operation.cancel()
            except BaseException as error:
                self._mark_poisoned(operation)
                if failure is None:
                    failure = error
        deadline = time.monotonic() + _LOGOUT_WAIT
        unfinished: set[int] = set()
        for operation in operations:
            try:
                if not operation.done.wait(max(0.0, deadline - time.monotonic())):
                    unfinished.add(id(operation))
            except BaseException as error:
                unfinished.add(id(operation))
                if failure is None:
                    failure = error
        for operation in operations:
            if id(operation) in unfinished or operation.kind not in {"auth", "auth_prepare"}:
                continue
            try:
                clear_credentials(self.runtime.root, operation.connection_id)
            except BaseException as error:
                self._mark_poisoned(operation)
                if failure is None:
                    failure = error
        with self._lock:
            poisoned = bool(self._poisoned_connections)
        if failure is not None:
            if isinstance(failure, Exception):
                raise unavailable(PROCESS_CLEANUP_REASON) from None
            raise failure
        if unfinished or poisoned:
            raise unavailable(PROCESS_CLEANUP_REASON)

    def _raise_if_poisoned(self, connection_id: str) -> None:
        with self._lock:
            poisoned = connection_id in self._poisoned_connections
        if poisoned:
            raise unavailable(PROCESS_CLEANUP_REASON)

    def _mark_poisoned(self, source: _Operation) -> None:
        with self._lock:
            self._poisoned_connections.add(source.connection_id)
