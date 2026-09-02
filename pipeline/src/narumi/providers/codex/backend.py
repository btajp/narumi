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
from narumi.providers.codex._rpc import StdioRPC, unavailable
from narumi.providers.codex._runtime import CodexRuntime
from narumi.providers.codex._session import CodexSession, clear_credentials, connection_directory

AUTH_TIMEOUT = 600.0
_LOGOUT_WAIT = 5.0


@dataclass
class _Operation:
    kind: str
    closed: threading.Event
    callback: Callable[[], bool]
    operation_id: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    rpc: StdioRPC | None = None

    def should_cancel(self) -> bool:
        return self.closed.is_set() or self.cancelled.is_set() or self.callback()

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
        self.cancelled.set()
        with self.lock:
            rpc = self.rpc
        if rpc is not None:
            rpc.cancel()


class CodexBackend:
    """No process, credential lookup or file mutation during construction/listing."""

    def __init__(self, root: Path) -> None:
        self.runtime = CodexRuntime(root)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._operations: dict[str, _Operation] = {}

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
    ) -> Iterator[_Operation]:
        connection_directory(self.runtime.root, connection_id)
        with self._lock:
            if self._closed.is_set():
                raise unavailable("codex_backend_closed")
            if connection_id in self._operations:
                raise BusyError("This Codex connection already has an active operation")
            operation = _Operation(kind, self._closed, cancelled or (lambda: False), operation_id)
            self._operations[connection_id] = operation
        try:
            if operation.should_cancel():
                raise CancelledError("Codex operation was cancelled")
            yield operation
        except NarumiError:
            raise
        except Exception:
            raise unavailable("codex_operation_failed") from None
        finally:
            operation.cancel()
            with self._lock:
                if self._operations.get(connection_id) is operation:
                    del self._operations[connection_id]
                operation.done.set()

    def authenticate(
        self,
        connection_id: str,
        *,
        on_authorization_code: Callable[[str, str], None],
        cancelled: Callable[[], bool],
        operation_id: str | None = None,
    ) -> None:
        with self._operation(
            connection_id, "auth", cancelled=cancelled, operation_id=operation_id
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

    def list_models(self, connection_id: str) -> list[dict[str, Any]]:
        with self._operation(connection_id, "models") as operation:
            with CodexSession(self.runtime, connection_id, operation) as session:
                session.require_chatgpt()
                session.verify_configuration()
                return _models.fetch_models(session.call)

    def cancel_auth(self, connection_id: str, *, operation_id: str | None = None) -> bool:
        """Return true only when this request performed verified credential cleanup."""
        with self._lock:
            operation = self._operations.get(connection_id)
            if operation is not None and (
                operation.kind != "auth"
                or (operation_id is not None and operation.operation_id != operation_id)
            ):
                # A stale operation ID must never cancel or clean a newer session.
                return False
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
        with self._operation(connection_id, "cancel_cleanup"):
            clear_credentials(self.runtime.root, connection_id)
        return True

    def logout(self, connection_id: str) -> None:
        with self._lock:
            previous = self._operations.get(connection_id)
        if previous is not None:
            if previous.kind != "auth":
                raise BusyError("This Codex connection is in use")
            previous.cancel()
            if not previous.done.wait(_LOGOUT_WAIT):
                raise BusyError("Codex authentication has not finished cancelling")
        with self._operation(connection_id, "logout") as operation:
            if operation.should_cancel():
                raise CancelledError("Codex logout was cancelled")
            clear_credentials(self.runtime.root, connection_id)

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
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            operations = list(self._operations.values())
        for operation in operations:
            operation.cancel()
