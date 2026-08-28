"""Compare-and-set connection mutations, with fail-closed credential replacement."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from narumi.errors import BusyError, InvalidArgumentError
from narumi.providers._common import (
    cancel_auth,
    check_provider_idle,
    check_revision,
    connection,
    invalidate_checks,
    public_connection,
)
from narumi.providers._requests import safe_secret_error
from narumi.providers.metadata import validate_endpoint

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService


class Connections:
    def __init__(self, service: ProviderService) -> None:
        self.service = service

    def set(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        self._validate_key(args.get("api_key"))
        if isinstance(args.get("api_key"), str) and args["api_key"] in json.dumps(
            {key: value for key, value in args.items() if key != "api_key"},
            ensure_ascii=False,
        ):
            raise InvalidArgumentError(
                "Provider credentials cannot also be used as public metadata"
            )
        with service.store.transaction() as document:
            fingerprint = service.requests.fingerprint("set_provider_connection", args)
            replay = service.requests.replay(document, args, fingerprint)
            if replay is not None:
                return replay
            if "connection_id" in args:
                record = connection(document, args["connection_id"])
                check_revision(record, args["expected_revision"])
                disabling_only = args.get("enabled") is False and set(args) <= {
                    "connection_id",
                    "expected_revision",
                    "request_id",
                    "enabled",
                }
                if not disabling_only:
                    check_provider_idle(document, record["provider_id"])
                record["revision"] += 1
            else:
                check_provider_idle(document, args["provider_id"])
                record = self._new(args)
                document["connections"][record["connection_id"]] = record
            self._update_fields(document, record, args)
            receipt = service.requests.accept(document, args, fingerprint)
            if "api_key" in args:
                self._replace_credential(document, record, receipt, args["api_key"])
            response = {"connection": public_connection(record)}
            return service.requests.complete(receipt, response)

    def delete(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        with service.store.transaction() as document:
            fingerprint = service.requests.fingerprint("delete_provider_connection", args)
            replay = service.requests.replay(document, args, fingerprint)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            check_provider_idle(document, record["provider_id"])
            if service.connection_referenced(record["connection_id"]):
                raise BusyError("Provider connection is still referenced by a saved profile")
            receipt = service.requests.accept(document, args, fingerprint)
            record["enabled"] = False
            record["revision"] += 1
            self._replace_credential(document, record, receipt, None)
            del document["connections"][record["connection_id"]]
            document["catalogs"].pop(record["connection_id"], None)
            return service.requests.complete(
                receipt,
                {
                    "connection_id": record["connection_id"],
                    "deleted": True,
                },
            )

    @staticmethod
    def _new(args: dict[str, Any]) -> dict[str, Any]:
        provider_id = args["provider_id"]
        endpoint = args.get("endpoint") or (
            "http://127.0.0.1:11434" if provider_id == "ollama" else "https://api.anthropic.com"
        )
        return {
            "connection_id": "conn-" + uuid.uuid4().hex,
            "revision": 1,
            "provider_id": provider_id,
            "display_name": args["display_name"],
            "enabled": True,
            "endpoint": validate_endpoint(provider_id, endpoint),
            "auth_method": "none" if provider_id == "ollama" else "api_key",
            "credential_present": False,
            "auth_state": "unverified" if provider_id == "ollama" else "unconfigured",
            "catalog_state": "unfetched",
            "checked_at": None,
            "active_auth": None,
            "last_generation_state": "never",
            "secret_account": None,
            "pending_secret_accounts": [],
        }

    @staticmethod
    def _update_fields(
        document: dict[str, Any],
        record: dict[str, Any],
        args: dict[str, Any],
    ) -> None:
        auth_method = "none" if record["provider_id"] == "ollama" else "api_key"
        if args.get("auth_method", auth_method) != auth_method:
            raise InvalidArgumentError(
                "The authentication method is not supported by this provider"
            )
        if auth_method == "none" and args.get("api_key") is not None:
            raise InvalidArgumentError("This provider does not accept API credentials")
        if "display_name" in args:
            if not args["display_name"].strip():
                raise InvalidArgumentError("A provider connection display name is required")
            record["display_name"] = args["display_name"]
        if "endpoint" in args:
            endpoint = validate_endpoint(record["provider_id"], args["endpoint"])
            if endpoint != record["endpoint"]:
                if record["credential_present"] and "api_key" not in args:
                    raise InvalidArgumentError(
                        "Endpoint changes require an explicit credential update"
                    )
                record["endpoint"] = endpoint
                invalidate_checks(document, record)
        if "enabled" in args:
            reenabled = args["enabled"] and not record["enabled"]
            record["enabled"] = args["enabled"]
            if not record["enabled"]:
                cancel_auth(document, record, "connection_disabled")
            elif reenabled:
                invalidate_checks(document, record)

    def _replace_credential(
        self,
        document: dict[str, Any],
        record: dict[str, Any],
        receipt: dict[str, Any],
        value: str | None,
    ) -> None:
        service = self.service
        old_account = record.get("secret_account")
        cleanup_accounts = set(record.get("pending_secret_accounts", []))
        if old_account is not None:
            cleanup_accounts.add(old_account)
        new_account = (
            None
            if value is None
            else (f"providers:{service.namespace}:{record['connection_id']}:{uuid.uuid4().hex}")
        )
        # Make the old key unreachable before touching Keychain. If either store fails,
        # only an explicit later credential update may restore use of this connection.
        record.update(secret_account=None, credential_present=False)
        record["pending_secret_accounts"] = sorted(
            cleanup_accounts | ({new_account} if new_account is not None else set())
        )
        invalidate_checks(document, record)
        service.store.commit(document)
        try:
            if new_account is not None and value is not None:
                service.secrets.set(new_account, value)
            for account in cleanup_accounts:
                service.secrets.delete(account)
        except Exception:
            if new_account is not None:
                try:
                    service.secrets.delete(new_account)
                except Exception:
                    pass
            service.requests.fail(receipt)
            service.store.commit(document)
            raise safe_secret_error() from None
        record.update(secret_account=new_account, credential_present=new_account is not None)
        record["pending_secret_accounts"] = []
        invalidate_checks(document, record)

    @staticmethod
    def _validate_key(value: Any) -> None:
        if value is None:
            return
        if (
            not isinstance(value, str)
            or not value
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise InvalidArgumentError("Provider API credentials must contain printable ASCII only")
