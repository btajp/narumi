"""Persisted, connection-scoped API verification; no browser or ambient SDK login."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from narumi.errors import BusyError, InvalidArgumentError, NarumiError, NotFoundError
from narumi.providers._common import (
    cancel_auth,
    check_provider_idle,
    check_revision,
    connection,
    timestamp,
)

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService


class Authentication:
    def __init__(self, service: ProviderService) -> None:
        self.service = service

    def authenticate(self, args: dict[str, Any]) -> dict[str, Any]:
        if args["action"] == "start":
            return self._start(args)
        if args["action"] == "cancel":
            return self._cancel(args)
        return self._logout(args)

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        document = self.service.store.read()
        operation = self._lookup(document, args)
        return {"operation": copy.deepcopy(operation)}

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
            operation = self._operation(record, args)
            document["auth_operations"][operation["operation_id"]] = operation
            record["active_auth"] = self._active(operation)
            record["auth_state"] = "authenticating"
            document["checks"][record["provider_id"]] = {
                "token": operation["operation_id"],
                "server_instance_id": service.server_instance_id,
                "connection_id": record["connection_id"],
            }
            receipt = service.requests.accept(document, args, fingerprint)
            response = service.requests.complete(receipt, {"operation": operation})
            snapshot = copy.deepcopy(record)
        try:
            service.auth_executor.submit(self._verify, operation["operation_id"], snapshot)
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
                self.service.catalog.release_check(document, snapshot["provider_id"], operation_id)
                operation = document["auth_operations"].get(operation_id)
                if operation is None or operation["state"] != "pending":
                    return
                operation.update(
                    state="failed",
                    reason="authentication_verification_unavailable",
                    updated_at=timestamp(),
                )
                record = document["connections"].get(snapshot["connection_id"])
                if (
                    record is not None
                    and record["active_auth"] is not None
                    and (record["active_auth"]["operation_id"] == operation_id)
                ):
                    record.update(active_auth=None, auth_state="failed")
        except Exception:
            pass

    def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        with service.store.transaction() as document:
            fingerprint, replay = self._replay(document, args)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            operation = self._lookup(document, args)
            if operation["state"] in ("pending", "unknown"):
                operation.update(
                    state="cancelled", reason="authentication_cancelled", updated_at=timestamp()
                )
                active = record["active_auth"]
                if active is not None and active["operation_id"] == operation["operation_id"]:
                    record["active_auth"] = None
                    record["auth_state"] = (
                        "unverified"
                        if record["credential_present"] or record["auth_method"] == "none"
                        else "unconfigured"
                    )
            receipt = service.requests.accept(document, args, fingerprint)
            return service.requests.complete(receipt, {"operation": operation})

    def _logout(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        with service.store.transaction() as document:
            fingerprint, replay = self._replay(document, args)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            check = document["checks"].get(record["provider_id"])
            if check is not None and check["connection_id"] != record["connection_id"]:
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
        fingerprint = self.service.requests.fingerprint("authenticate_provider_connection", args)
        replay = self.service.requests.replay(document, args, fingerprint)
        if replay is not None:
            operation = document["auth_operations"].get(replay["operation"]["operation_id"])
            if operation is not None:
                replay = {"operation": copy.deepcopy(operation)}
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
