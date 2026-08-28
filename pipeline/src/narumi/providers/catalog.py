"""Explicit metadata checks and connection-scoped, paginated model observations."""

from __future__ import annotations

import base64
import copy
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from narumi.errors import (
    ConfigurationConflictError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
)
from narumi.providers._common import (
    check_provider_idle,
    check_revision,
    connection,
    public_connection,
    timestamp,
)
from narumi.providers.metadata import validate_endpoint

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService

PAGE_SIZE = 100


@dataclass(frozen=True)
class CheckResult:
    models: list[dict[str, Any]] | None
    reason: str | None
    authentication_failed: bool = False


class ModelCatalog:
    def __init__(self, service: ProviderService) -> None:
        self.service = service

    def test(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        token = uuid.uuid4().hex
        with service.store.transaction() as document:
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            if not record["enabled"]:
                return {
                    "connection": public_connection(record),
                    "connected": False,
                    "reason": "connection_disabled",
                }
            check_provider_idle(document, record["provider_id"])
            snapshot = copy.deepcopy(record)
            document["checks"][record["provider_id"]] = {
                "token": token,
                "server_instance_id": service.server_instance_id,
                "connection_id": record["connection_id"],
            }
        result = self.fetch(snapshot)
        with service.store.transaction() as document:
            self.release_check(document, snapshot["provider_id"], token)
            record = connection(document, snapshot["connection_id"])
            if not self.same_configuration(record, snapshot):
                service.store.commit(document)
                raise ConfigurationConflictError("Provider connection changed during verification")
            self.apply(document, record, result)
            return {
                "connection": public_connection(record),
                "connected": result.models is not None,
                "reason": result.reason,
            }

    def fetch(self, snapshot: dict[str, Any]) -> CheckResult:
        """No state mutation and no generation. Discard all untrusted exception contents."""
        service = self.service
        credential = None
        if snapshot["auth_method"] == "api_key":
            account = snapshot.get("secret_account")
            if not snapshot["credential_present"] or account is None:
                return CheckResult(None, "credential_required", True)
            try:
                credential = service.secrets.get(account)
            except Exception:
                return CheckResult(None, "credential_unavailable", True)
            if credential is None:
                return CheckResult(None, "credential_required", True)
        try:
            endpoint = validate_endpoint(snapshot["provider_id"], snapshot["endpoint"])
            models = service.metadata.fetch(snapshot["provider_id"], endpoint, credential)
            payload = {
                "connection_id": snapshot["connection_id"],
                "connection_revision": snapshot["revision"],
                "models": models,
                "next_cursor": None,
                "catalog_state": "ready",
                "fetched_at": timestamp(),
            }
            service.contracts.validate_output("list_provider_models", payload)
            # Metadata is untrusted even when structurally valid. A provider echoing the
            # credential must not turn it into cached display text or an exception value.
            if credential and credential in json.dumps(models, ensure_ascii=False):
                return CheckResult(None, "metadata_response_rejected")
            if len({model["model_id"] for model in models}) != len(models):
                return CheckResult(None, "metadata_response_rejected")
            return CheckResult(copy.deepcopy(models), None)
        except NarumiError as error:
            if error.code == ErrorCode.AUTHENTICATION_REQUIRED:
                return CheckResult(None, "credential_rejected", True)
            return CheckResult(None, "metadata_unavailable")
        except Exception:
            return CheckResult(None, "metadata_unavailable")

    @staticmethod
    def apply(
        document: dict[str, Any],
        record: dict[str, Any],
        result: CheckResult,
    ) -> None:
        checked_at = timestamp()
        record["checked_at"] = checked_at
        if result.models is not None:
            record.update(auth_state="authenticated", catalog_state="ready")
            document["catalogs"][record["connection_id"]] = {
                "models": copy.deepcopy(result.models),
                "fetched_at": checked_at,
                "connection_revision": record["revision"],
                "catalog_id": uuid.uuid4().hex,
            }
            return
        record["auth_state"] = "failed" if result.authentication_failed else "unverified"
        if result.authentication_failed:
            record["catalog_state"] = "authentication_required"
        elif record["connection_id"] in document["catalogs"]:
            record["catalog_state"] = "stale"
        else:
            record["catalog_state"] = "failed"

    @staticmethod
    def release_check(document: dict[str, Any], provider_id: str, token: str) -> None:
        check = document["checks"].get(provider_id)
        if check is not None and check["token"] == token:
            del document["checks"][provider_id]

    @staticmethod
    def same_configuration(record: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        return (
            record["revision"] == snapshot["revision"]
            and record["enabled"]
            and (record.get("secret_account") == snapshot.get("secret_account"))
        )

    def list_models(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("refresh"):
            if args.get("cursor") is not None:
                raise InvalidArgumentError("A model refresh cannot reuse a pagination cursor")
            record = connection(self.service.store.read(), args["connection_id"])
            self.test(
                {"connection_id": record["connection_id"], "expected_revision": record["revision"]}
            )
        document = self.service.store.read()
        record = connection(document, args["connection_id"])
        cached = document["catalogs"].get(record["connection_id"], {})
        role = args.get("role", "llm")
        identity = [record["connection_id"], record["revision"], role, cached.get("catalog_id")]
        offset = self._cursor_offset(args.get("cursor"), identity)
        models = [
            copy.deepcopy(model)
            for model in cached.get("models", [])
            if role in model["roles"] or not model["roles"]
        ]
        if offset > len(models):
            raise InvalidArgumentError("Model pagination cursor is invalid")
        if record["catalog_state"] != "ready":
            for model in models:
                if model["availability"] == "available":
                    model.update(availability="unverified", reason="model_catalog_stale")
        next_cursor = None
        if offset + PAGE_SIZE < len(models):
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [*identity, offset + PAGE_SIZE],
                    separators=(",", ":"),
                ).encode()
            ).decode()
        return {
            "connection_id": record["connection_id"],
            "connection_revision": record["revision"],
            "models": models[offset : offset + PAGE_SIZE],
            "next_cursor": next_cursor,
            "catalog_state": record["catalog_state"],
            "fetched_at": cached.get("fetched_at"),
        }

    @staticmethod
    def _cursor_offset(cursor: str | None, identity: list[Any]) -> int:
        if cursor is None:
            return 0
        try:
            decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(decoded, list) or len(decoded) != 5 or decoded[:4] != identity:
                raise ValueError
            offset = decoded[4]
            if type(offset) is not int or offset < 0 or offset % PAGE_SIZE:
                raise ValueError
            return offset
        except (ValueError, UnicodeError, TypeError):
            raise InvalidArgumentError("Model pagination cursor is invalid or stale") from None
