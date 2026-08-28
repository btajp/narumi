"""Durable official login operations for a connection's isolated Codex session."""

from __future__ import annotations

import copy
import re
import threading
from typing import TYPE_CHECKING, Any

from narumi.errors import (
    AuthenticationRequiredError,
    BusyError,
    CancelledError,
    EngineUnavailableError,
    NarumiError,
)
from narumi.providers._common import (
    cancel_auth,
    check_revision,
    connection,
    invalidate_checks,
    timestamp,
)

if TYPE_CHECKING:
    from narumi.providers.auth import Authentication
    from narumi.providers.service import ProviderService


class CodexAuthentication:
    def __init__(self, service: ProviderService, auth: Authentication) -> None:
        self.service = service
        self.auth = auth
        # These short-lived UI values must never enter a durable operation or receipt.
        self._challenges: dict[str, dict[str, str]] = {}
        self._challenge_lock = threading.Lock()

    def forget(self, operation_id: str | None = None) -> None:
        with self._challenge_lock:
            if operation_id is None:
                self._challenges.clear()
            else:
                self._challenges.pop(operation_id, None)

    def public_operation(
        self, document: dict[str, Any], operation: dict[str, Any]
    ) -> dict[str, Any]:
        public = {**copy.deepcopy(operation), "authorization_url": None, "user_code": None}
        record = document["connections"].get(operation["connection_id"])
        with self._challenge_lock:
            if (
                not self.service.closed.is_set()
                and operation["action"] == "start"
                and record is not None
                and record["provider_id"] == "codex-app-server"
                and self._matches(operation, record, {"revision": operation["connection_revision"]})
            ):
                public.update(self._challenges.get(operation["operation_id"], {}))
            else:
                self._challenges.pop(operation["operation_id"], None)
        return public

    def require_ready(self, document: dict[str, Any]) -> None:
        runtime = self.service.runtime._current("codex-app-server", document)
        if runtime["state"] != "ready":
            raise EngineUnavailableError("Prepare the Codex runtime before authentication")

    def login(self, operation_id: str, snapshot: dict[str, Any]) -> None:
        self._run(operation_id, snapshot, login=True)

    def _run(self, operation_id: str, snapshot: dict[str, Any], *, login: bool) -> None:
        try:
            if not self._pending(operation_id, snapshot):
                self._finish(operation_id, snapshot, "cancelled", "authentication_cancelled")
                return
            if login:
                self.service.codex_backend.authenticate(
                    snapshot["connection_id"],
                    on_authorization_code=lambda url, code: self._publish_code(
                        operation_id, snapshot, url, code
                    ),
                    cancelled=lambda: not self._pending(operation_id, snapshot),
                    operation_id=operation_id,
                )
            else:
                self.service.codex_backend.logout(snapshot["connection_id"])
            self._finish(operation_id, snapshot, "succeeded", None)
        except AuthenticationRequiredError as error:
            reason = (
                "device_code_login_unavailable"
                if error.details.get("reason") == "device_code_login_unavailable"
                else "credential_rejected"
            )
            self._finish(operation_id, snapshot, "failed", reason)
        except CancelledError:
            self._finish(operation_id, snapshot, "cancelled", "authentication_cancelled")
        except Exception:
            # Neither an upstream error nor a private session path may enter the registry.
            self.auth._submission_failed(operation_id, snapshot)
        finally:
            self.forget(operation_id)

    def _pending(self, operation_id: str, snapshot: dict[str, Any]) -> bool:
        if self.service.closed.is_set():
            return False
        try:
            document = self.service.store.read()
            operation = document["auth_operations"].get(operation_id)
            record = document["connections"].get(snapshot["connection_id"])
            return operation is not None and self._matches(operation, record, snapshot)
        except Exception:
            return False

    def _matches(
        self,
        operation: dict[str, Any],
        record: dict[str, Any] | None,
        snapshot: dict[str, Any],
    ) -> bool:
        if record is None or record["active_auth"] is None:
            return False
        return (
            operation["state"] == "pending"
            and operation["server_instance_id"] == self.service.server_instance_id
            and record["revision"] == snapshot["revision"]
            and record["active_auth"]["operation_id"] == operation["operation_id"]
            and (record["enabled"] or operation["action"] == "logout")
        )

    def _publish_code(
        self, operation_id: str, snapshot: dict[str, Any], url: str, code: str
    ) -> None:
        if (
            url != "https://auth.openai.com/codex/device"
            or not isinstance(code, str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,32}", code) is None
        ):
            raise NarumiError("Codex device authorization could not be verified")
        with self.service.store.transaction() as document:
            operation = document["auth_operations"].get(operation_id)
            record = document["connections"].get(snapshot["connection_id"])
            if (
                self.service.closed.is_set()
                or operation is None
                or not self._matches(operation, record, snapshot)
            ):
                raise CancelledError("Provider authentication was cancelled")
            challenge = {"authorization_url": url, "user_code": code, "updated_at": timestamp()}
            try:
                self.service.contracts.validate_output(
                    "get_provider_auth_status", {"operation": {**operation, **challenge}}
                )
            except Exception:
                raise NarumiError("Codex device authorization could not be verified") from None
            # Keep lock ordering store -> challenge. Close never waits on the store while
            # holding this lock, and a late callback cannot restore a cleared challenge.
            with self._challenge_lock:
                if self.service.closed.is_set():
                    raise CancelledError("Provider authentication was cancelled")
                self._challenges[operation_id] = challenge

    def _finish(
        self,
        operation_id: str,
        snapshot: dict[str, Any],
        state: str,
        reason: str | None,
    ) -> None:
        with self.service.store.transaction() as document:
            self.service.catalog.release_check(document, snapshot["provider_id"], operation_id)
            operation = document["auth_operations"].get(operation_id)
            if operation is None or operation["state"] != "pending":
                return
            record = document["connections"].get(snapshot["connection_id"])
            if self.service.closed.is_set():
                state, reason = "unknown", "authentication_operation_interrupted"
            elif not self._matches(operation, record, snapshot):
                state, reason = "cancelled", "connection_configuration_changed"
            operation.update(
                state=state,
                reason=reason,
                authorization_url=None,
                user_code=None,
                updated_at=timestamp(),
            )
            self.forget(operation_id)
            if (
                record is None
                or record["active_auth"] is None
                or (record["active_auth"]["operation_id"] != operation_id)
            ):
                return
            if state == "unknown":
                record.update(auth_state="unknown", active_auth=self.auth._active(operation))
            else:
                authenticated = state == "succeeded" and operation["action"] == "start"
                record.update(
                    credential_present=authenticated,
                    active_auth=None,
                    auth_state="authenticated"
                    if authenticated
                    else ("failed" if state == "failed" else "unconfigured"),
                    checked_at=timestamp() if authenticated else None,
                )

    def logout(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        with service.store.transaction() as document:
            fingerprint, replay = self.auth._replay(document, args)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            check = document["checks"].get(record["provider_id"])
            if check is not None and (
                check["connection_id"] != record["connection_id"]
                or check.get("kind") != "authentication"
            ):
                raise BusyError("Provider work is active")
            runtime = document["runtimes"].get(record["provider_id"], {})
            active_setup = runtime.get("active_setup")
            if runtime.get("pending_submission") or (
                active_setup is not None and active_setup["state"] in ("queued", "running")
            ):
                raise BusyError("Provider runtime preparation is active")
            active = record["active_auth"]
            cancel_auth(document, record, "connection_logged_out")
            if active is not None:
                self.forget(active["operation_id"])
            record["revision"] += 1
            record["credential_present"] = False
            invalidate_checks(document, record)
            operation = self.auth._operation(record, args)
            document["auth_operations"][operation["operation_id"]] = operation
            record["active_auth"] = self.auth._active(operation)
            document["checks"][record["provider_id"]] = {
                "token": operation["operation_id"],
                "server_instance_id": service.server_instance_id,
                "connection_id": record["connection_id"],
                "kind": "logout",
            }
            receipt = service.requests.accept(document, args, fingerprint)
            response = service.requests.complete(receipt, {"operation": operation})
            snapshot = copy.deepcopy(record)
        try:
            service.codex_backend.cancel_auth(snapshot["connection_id"])
            service.auth_executor.submit(self._run_logout, operation["operation_id"], snapshot)
        except Exception:
            self.auth._submission_failed(operation["operation_id"], snapshot)
            raise NarumiError("Provider logout could not be started") from None
        return response

    def _run_logout(self, operation_id: str, snapshot: dict[str, Any]) -> None:
        self._run(operation_id, snapshot, login=False)
