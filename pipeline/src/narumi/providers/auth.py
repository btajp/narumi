"""Persisted API verification and explicit, isolated Codex login operations."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from narumi.errors import BusyError, InvalidArgumentError, NarumiError, NotFoundError
from narumi.providers._codex_auth import CodexAuthentication
from narumi.providers._common import (
    cancel_auth,
    check_provider_idle,
    check_revision,
    connection,
    invalidate_checks,
    timestamp,
)

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService


class Authentication:
    def __init__(self, service: ProviderService) -> None:
        self.service = service
        self.codex = CodexAuthentication(service, self)

    def authenticate(self, args: dict[str, Any]) -> dict[str, Any]:
        if args["action"] == "start":
            return self._start(args)
        if args["action"] == "cancel":
            return self._cancel(args)
        return self._logout(args)

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        document = self.service.store.read()
        operation = self._lookup(document, args)
        self.service.requests.reject_secret_identifiers(
            document,
            {
                "connection_id": args["connection_id"],
                "start_request_id": operation["start_request_id"],
            },
        )
        return {"operation": self.codex.public_operation(document, operation)}

    def _start(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        with service.store.transaction() as document:
            fingerprint, replay = self._replay(document, args)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            if not record["enabled"]:
                raise InvalidArgumentError("Enable this provider connection before authenticating")
            check_provider_idle(document, record["provider_id"])
            if record["provider_id"] == "codex-app-server":
                self.codex.require_ready(document)
                invalidate_checks(document, record)
            operation = self._operation(record, args)
            if record["provider_id"] == "codex-app-server" and not (
                service.codex_backend.register_auth_generation(
                    record["connection_id"],
                    operation_id=operation["operation_id"],
                    replace=True,
                    cleanup_required=False,
                )
            ):
                raise NarumiError("Codex authentication generation could not be registered")
            document["auth_operations"][operation["operation_id"]] = operation
            record["active_auth"] = self._active(operation)
            record["auth_state"] = "authenticating"
            document["checks"][record["provider_id"]] = {
                "token": operation["operation_id"],
                "server_instance_id": service.server_instance_id,
                "connection_id": record["connection_id"],
                "kind": "authentication",
            }
            receipt = service.requests.accept(document, args, fingerprint)
            response = service.requests.complete(receipt, {"operation": operation})
            snapshot = copy.deepcopy(record)
        try:
            worker = (
                self.codex.login if snapshot["provider_id"] == "codex-app-server" else self._verify
            )
            service.auth_executor.submit(worker, operation["operation_id"], snapshot)
        except Exception:
            self._submission_failed(operation["operation_id"], snapshot)
            raise NarumiError("Provider authentication could not be started") from None
        return response

    def _verify(self, operation_id: str, snapshot: dict[str, Any]) -> None:
        service = self.service
        try:
            with service.store.transaction() as document:
                operation = document["auth_operations"][operation_id]
                if operation["state"] != "pending" or service.closed.is_set():
                    service.catalog.release_check(document, snapshot["provider_id"], operation_id)
                    return
            result = service.catalog.fetch(snapshot)
            with service.store.transaction() as document:
                service.catalog.release_check(document, snapshot["provider_id"], operation_id)
                operation = document["auth_operations"][operation_id]
                if operation["state"] != "pending":
                    return
                record = connection(document, snapshot["connection_id"])
                if service.closed.is_set():
                    operation.update(state="unknown", reason="authentication_operation_interrupted")
                    record["auth_state"] = "unknown"
                    record["active_auth"] = self._active(operation)
                elif not service.catalog.same_configuration(record, snapshot):
                    operation.update(state="cancelled", reason="connection_configuration_changed")
                    record["active_auth"] = None
                else:
                    service.catalog.apply(document, record, result)
                    operation.update(
                        state="succeeded" if result.models is not None else "failed",
                        reason=result.reason,
                    )
                    record["active_auth"] = None
                operation["updated_at"] = timestamp()
        except Exception:
            # If persistence is unavailable the accepted operation stays unresolved. Never
            # leak an upstream response through the executor or turn this into a new login.
            self._submission_failed(operation_id, snapshot)

    def _submission_failed(self, operation_id: str, snapshot: dict[str, Any]) -> None:
        try:
            with self.service.store.transaction() as document:
                operation = document["auth_operations"].get(operation_id)
                if operation is None or operation["state"] != "pending":
                    return
                record = document["connections"].get(snapshot["connection_id"])
                if (
                    snapshot["provider_id"] == "codex-app-server"
                    and operation["action"] == "logout"
                ):
                    operation.update(
                        state="unknown",
                        authorization_url=None,
                        user_code=None,
                        reason="authentication_operation_interrupted",
                        updated_at=timestamp(),
                    )
                    if (
                        record is not None
                        and record["active_auth"] is not None
                        and record["active_auth"]["operation_id"] == operation_id
                    ):
                        record.update(
                            active_auth=self._active(operation),
                            auth_state="unknown",
                            checked_at=None,
                        )
                    return
                self.service.catalog.release_check(document, snapshot["provider_id"], operation_id)
                operation.update(
                    state="failed",
                    authorization_url=None,
                    user_code=None,
                    reason="authentication_verification_unavailable",
                    updated_at=timestamp(),
                )
                if (
                    record is not None
                    and record["active_auth"] is not None
                    and (record["active_auth"]["operation_id"] == operation_id)
                ):
                    record.update(active_auth=None, auth_state="failed")
        except Exception:
            pass
        finally:
            self.codex.forget(operation_id)

    def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        codex_cleanup_action = None
        cleanup_required = False
        cancellation_token = None
        with service.store.transaction() as document:
            fingerprint, replay = self._replay(document, args)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            operation = self._lookup(document, args)
            if operation["state"] in ("pending", "unknown"):
                active = record["active_auth"]
                if (
                    record["provider_id"] == "codex-app-server"
                    and active is not None
                    and active["operation_id"] == operation["operation_id"]
                    and operation["action"] in ("start", "logout")
                ):
                    codex_cleanup_action = operation["action"]
                    cleanup_required = (
                        codex_cleanup_action == "start" and not record["credential_present"]
                    )
                    check = document["checks"].get(record["provider_id"])
                    if check is not None and (
                        check["connection_id"] != record["connection_id"]
                        or check.get("kind")
                        != ("authentication" if codex_cleanup_action == "start" else "logout")
                        or check["token"] != operation["operation_id"]
                    ):
                        raise BusyError("Provider authentication cancellation is active")
                    cancellation_token = uuid.uuid4().hex
                    self._mark_codex_cancellation_unknown(
                        document,
                        record,
                        operation["operation_id"],
                        reason="authentication_cancellation_unresolved",
                    )
                    document["checks"][record["provider_id"]] = {
                        "token": cancellation_token,
                        "server_instance_id": service.server_instance_id,
                        "connection_id": record["connection_id"],
                        "kind": "authentication_cancel",
                    }
                else:
                    self._finish_codex_cancellation(
                        document,
                        record,
                        operation["operation_id"],
                        reason="authentication_cancelled",
                    )
            self.codex.forget(operation["operation_id"])
            receipt = service.requests.accept(document, args, fingerprint)
            if codex_cleanup_action is None:
                response = service.requests.complete(receipt, {"operation": operation})
        if codex_cleanup_action is not None:
            try:
                if codex_cleanup_action == "logout":
                    service.codex_backend.logout(args["connection_id"])
                else:
                    registered = service.codex_backend.register_auth_generation(
                        args["connection_id"],
                        operation_id=operation["operation_id"],
                        replace=False,
                        cleanup_required=cleanup_required,
                    )
                    cleaned = registered and service.codex_backend.cancel_auth(
                        args["connection_id"], operation_id=operation["operation_id"]
                    )
                    if not cleaned:
                        raise NarumiError("Codex authentication generation is no longer current")
            except Exception:
                self._codex_cancellation_failed(
                    args["request_id"], record["provider_id"], cancellation_token
                )
                raise NarumiError("Provider authentication cancellation is unresolved") from None
            try:
                with service.store.transaction() as document:
                    service.catalog.release_check(
                        document, record["provider_id"], cancellation_token
                    )
                    current = connection(document, args["connection_id"])
                    check_revision(current, args["expected_revision"])
                    current_operation = self._finish_codex_cancellation(
                        document,
                        current,
                        operation["operation_id"],
                        reason="authentication_cancelled",
                    )
                    return service.requests.complete(
                        document["requests"][args["request_id"]],
                        {"operation": current_operation},
                    )
            except Exception:
                self._codex_cancellation_failed(
                    args["request_id"], record["provider_id"], cancellation_token
                )
                raise NarumiError(
                    "Provider authentication cancellation could not be confirmed"
                ) from None
        return response

    def _mark_codex_cancellation_unknown(
        self,
        document: dict[str, Any],
        record: dict[str, Any],
        operation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        operation = document["auth_operations"].get(operation_id)
        if operation is None:
            raise NarumiError("Provider authentication cancellation state is unavailable")
        operation.update(
            state="unknown",
            authorization_url=None,
            user_code=None,
            reason=reason,
            updated_at=timestamp(),
        )
        record["active_auth"] = self._active(operation)
        record["auth_state"] = "unknown"
        return operation

    def _finish_codex_cancellation(
        self,
        document: dict[str, Any],
        record: dict[str, Any],
        operation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        operation = document["auth_operations"].get(operation_id)
        if operation is None:
            raise NarumiError("Provider authentication cancellation state is unavailable")
        operation.update(
            state="cancelled",
            authorization_url=None,
            user_code=None,
            reason=reason,
            updated_at=timestamp(),
        )
        active = record.get("active_auth")
        if active is not None and active["operation_id"] == operation_id:
            record["active_auth"] = None
            record["auth_state"] = (
                "unverified"
                if record["credential_present"] or record["auth_method"] == "none"
                else "unconfigured"
            )
        return operation

    def _codex_cancellation_failed(
        self,
        request_id: str,
        provider_id: str,
        token: str | None,
    ) -> None:
        try:
            with self.service.store.transaction() as document:
                if token is not None:
                    self.service.catalog.release_check(document, provider_id, token)
                receipt = document["requests"].get(request_id)
                if receipt is not None and receipt.get("response") is None:
                    receipt["state"] = "unknown"
        except Exception:
            pass

    def _logout(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        record = service.store.read()["connections"].get(args["connection_id"])
        if record is not None and record["provider_id"] == "codex-app-server":
            return self.codex.logout(args)
        with service.store.transaction() as document:
            fingerprint, replay = self._replay(document, args)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            check = document["checks"].get(record["provider_id"])
            if check is not None and (
                check["connection_id"] != record["connection_id"]
                or check.get("kind") == "generation"
            ):
                raise BusyError("Another connection of this provider is being verified")
            runtime = document["runtimes"].get(record["provider_id"], {})
            active_setup = runtime.get("active_setup")
            if active_setup is not None and active_setup["state"] in ("queued", "running"):
                raise BusyError("Provider runtime preparation is active")
            cancel_auth(document, record, "connection_logged_out")
            record["revision"] += 1
            operation = self._operation(record, args)
            document["auth_operations"][operation["operation_id"]] = operation
            record["active_auth"] = self._active(operation)
            receipt = service.requests.accept(document, args, fingerprint)
            try:
                service.connections._replace_credential(document, record, receipt, None)
            except NarumiError:
                operation.update(
                    state="failed", reason="credential_update_failed", updated_at=timestamp()
                )
                record["active_auth"] = None
                service.store.commit(document)
                raise
            operation.update(state="succeeded", updated_at=timestamp())
            record["active_auth"] = None
            return service.requests.complete(receipt, {"operation": operation})

    def _replay(
        self,
        document: dict[str, Any],
        args: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any] | None]:
        fingerprint = self.service.requests.fingerprint(
            "authenticate_provider_connection", args, document=document
        )
        replay = self.service.requests.replay(document, args, fingerprint)
        if replay is not None:
            operation = document["auth_operations"].get(replay["operation"]["operation_id"])
            replay = {
                "operation": self.codex.public_operation(document, operation or replay["operation"])
            }
        return fingerprint, replay

    def _operation(self, record: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        now = timestamp()
        return {
            "operation_id": "auth-" + uuid.uuid4().hex,
            "connection_id": record["connection_id"],
            "connection_revision": record["revision"],
            "server_instance_id": self.service.server_instance_id,
            "start_request_id": args["request_id"],
            "action": args["action"],
            "state": "pending",
            "authorization_url": None,
            "user_code": None,
            "reason": None,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _active(operation: dict[str, Any]) -> dict[str, Any]:
        return {
            key: operation[key]
            for key in (
                "operation_id",
                "start_request_id",
                "server_instance_id",
                "state",
            )
        }

    @staticmethod
    def _lookup(document: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if "operation_id" in args:
            operation = document["auth_operations"].get(args["operation_id"])
            if operation is not None and operation["connection_id"] == args["connection_id"]:
                return operation
        else:
            for operation in document["auth_operations"].values():
                if operation["connection_id"] == args["connection_id"] and (
                    operation["start_request_id"] == args["start_request_id"]
                ):
                    return operation
        raise NotFoundError("Provider authentication operation was not found")
