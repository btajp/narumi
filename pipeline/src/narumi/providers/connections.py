"""Compare-and-set connection mutations, with fail-closed credential replacement."""

from __future__ import annotations

import copy
import ipaddress
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from narumi.errors import BusyError, InvalidArgumentError, NarumiError
from narumi.providers._common import (
    AUTH_METHODS,
    SUPPORTED_AUTH_METHODS,
    cancel_auth,
    check_provider_idle,
    check_revision,
    connection,
    invalidate_checks,
    public_connection,
)
from narumi.providers._requests import safe_secret_error
from narumi.providers.metadata import validate_endpoint
from narumi.providers.metadata.validation import check_public_payload

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService


class Connections:
    def __init__(self, service: ProviderService) -> None:
        self.service = service

    def set(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        cancel_codex = None
        credential = args.get("api_key")
        self._validate_key(credential)
        if isinstance(credential, str):
            try:
                check_public_payload(
                    {key: value for key, value in args.items() if key != "api_key"},
                    secrets=(credential,),
                    reject_credentials=False,
                )
            except NarumiError:
                raise InvalidArgumentError(
                    "Provider credentials cannot also be used as public metadata"
                ) from None
        with service.store.transaction() as document:
            saved = document["connections"].get(args.get("connection_id"))
            provider_id = saved["provider_id"] if saved is not None else args.get("provider_id")
            if provider_id == "codex-app-server" and "api_key" in args:
                raise InvalidArgumentError("This provider does not accept API credentials")
            known_credentials = service.requests.known_credentials(document)
            public_secrets = tuple(
                dict.fromkeys(
                    (*known_credentials, credential)
                    if isinstance(credential, str)
                    else known_credentials
                )
            )
            self._reject_secret_reflection(
                {key: value for key, value in args.items() if key != "api_key"},
                public_secrets,
            )
            fingerprint = service.requests.fingerprint(
                "set_provider_connection", args, document=document
            )
            self._reject_secret_reflection(fingerprint, public_secrets)
            replay = service.requests.replay(document, args, fingerprint)
            if replay is not None:
                self._reject_secret_reflection(replay, public_secrets)
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
                check = document["checks"].get(record["provider_id"], {})
                if check.get("kind") in ("logout", "delete", "authentication_cancel"):
                    raise BusyError("Provider credential cleanup is active")
                if disabling_only and any(
                    candidate["provider_id"] == record["provider_id"]
                    and candidate.get("pending_secret_accounts")
                    for candidate in document["connections"].values()
                ):
                    raise BusyError("Provider credential recovery is unresolved")
                if not disabling_only:
                    check_provider_idle(
                        document,
                        record["provider_id"],
                        credential_recovery_connection_id=(
                            record["connection_id"] if "api_key" in args else None
                        ),
                    )
                if record["provider_id"] == "codex-app-server" and args.get("enabled") is False:
                    active = record["active_auth"]
                    if active is not None and active["state"] in ("pending", "unknown"):
                        cancel_codex = (
                            record["connection_id"],
                            active["operation_id"],
                            uuid.uuid4().hex,
                        )
                record["revision"] += 1
            else:
                check_provider_idle(document, args["provider_id"])
                record = self._new(args)
                document["connections"][record["connection_id"]] = record
            self._update_fields(document, record, args)
            cached = document["catalogs"].get(record["connection_id"])
            if (
                record["catalog_state"] == "ready"
                and cached is not None
                and cached.get("connection_revision") != record["revision"]
            ):
                record["catalog_state"] = "stale"
            self._reject_secret_reflection(public_connection(record), public_secrets)
            if cancel_codex is not None:
                service.auth._mark_codex_cancellation_unknown(
                    document,
                    record,
                    cancel_codex[1],
                    reason="connection_disable_cancellation_unresolved",
                )
                document["checks"][record["provider_id"]] = {
                    "token": cancel_codex[2],
                    "server_instance_id": service.server_instance_id,
                    "connection_id": record["connection_id"],
                    "kind": "authentication_cancel",
                }
                service.auth.codex.forget(cancel_codex[1])
            receipt = service.requests.accept(document, args, fingerprint)
            if "api_key" in args:
                self._replace_credential(document, record, receipt, args["api_key"])
            if cancel_codex is None:
                response = {"connection": public_connection(record)}
                self._reject_secret_reflection(response, public_secrets)
                response = service.requests.complete(receipt, response)
            else:
                snapshot = copy.deepcopy(record)
        if cancel_codex is not None:
            try:
                registered = service.codex_backend.register_auth_generation(
                    cancel_codex[0],
                    operation_id=cancel_codex[1],
                    replace=False,
                    cleanup_required=not snapshot["credential_present"],
                )
                cleaned = registered and service.codex_backend.cancel_auth(
                    cancel_codex[0], operation_id=cancel_codex[1]
                )
                if not cleaned:
                    raise NarumiError("Codex authentication generation is no longer current")
            except Exception:
                service.auth._codex_cancellation_failed(
                    args["request_id"], snapshot["provider_id"], cancel_codex[2]
                )
                raise NarumiError("Provider authentication cancellation is unresolved") from None
            return self._finish_codex_disable(args, snapshot, cancel_codex[1], cancel_codex[2])
        return response

    def delete(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        with service.store.transaction() as document:
            fingerprint = service.requests.fingerprint(
                "delete_provider_connection", args, document=document
            )
            replay = service.requests.replay(document, args, fingerprint)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            check_provider_idle(
                document,
                record["provider_id"],
                credential_recovery_connection_id=record["connection_id"],
            )
            if service.connection_referenced(record["connection_id"]):
                raise BusyError(
                    "Provider connection is still referenced by a saved profile or meeting"
                )
            receipt = service.requests.accept(document, args, fingerprint)
            record["enabled"] = False
            record["revision"] += 1
            if record["provider_id"] == "codex-app-server":
                token = uuid.uuid4().hex
                record["credential_present"] = False
                invalidate_checks(document, record)
                document["checks"][record["provider_id"]] = {
                    "token": token,
                    "server_instance_id": service.server_instance_id,
                    "connection_id": record["connection_id"],
                    "kind": "delete",
                }
                snapshot = copy.deepcopy(record)
                # A failed cleanup must leave the old session unreachable to generation.
                service.store.commit(document)
            else:
                self._replace_credential(document, record, receipt, None)
                return self._finish_delete(document, record, receipt)
        return self._delete_codex(args, snapshot, token)

    def _finish_codex_disable(
        self,
        args: dict[str, Any],
        snapshot: dict[str, Any],
        operation_id: str,
        token: str,
    ) -> dict[str, Any]:
        service = self.service
        try:
            with service.store.transaction() as document:
                service.catalog.release_check(document, snapshot["provider_id"], token)
                record = connection(document, snapshot["connection_id"])
                check_revision(record, snapshot["revision"])
                service.auth._finish_codex_cancellation(
                    document,
                    record,
                    operation_id,
                    reason="connection_disabled",
                )
                return service.requests.complete(
                    document["requests"][args["request_id"]],
                    {"connection": public_connection(record)},
                )
        except Exception:
            service.auth._codex_cancellation_failed(
                args["request_id"], snapshot["provider_id"], token
            )
            raise NarumiError("Provider connection disable could not be confirmed") from None

    def _delete_codex(
        self, args: dict[str, Any], snapshot: dict[str, Any], token: str
    ) -> dict[str, Any]:
        service = self.service
        try:
            service.codex_backend.logout(snapshot["connection_id"])
        except Exception:
            with service.store.transaction() as document:
                service.catalog.release_check(document, snapshot["provider_id"], token)
                service.requests.fail(document["requests"][args["request_id"]])
            raise NarumiError("Provider session could not be removed securely") from None
        try:
            with service.store.transaction() as document:
                service.catalog.release_check(document, snapshot["provider_id"], token)
                record = connection(document, snapshot["connection_id"])
                check_revision(record, snapshot["revision"])
                return self._finish_delete(
                    document, record, document["requests"][args["request_id"]]
                )
        except Exception:
            try:
                with service.store.transaction() as document:
                    service.catalog.release_check(document, snapshot["provider_id"], token)
                    receipt = document["requests"][args["request_id"]]
                    if receipt["response"] is None:
                        service.requests.fail(receipt)
            except Exception:
                pass
            raise NarumiError("Provider connection deletion could not be confirmed") from None

    def _finish_delete(
        self, document: dict[str, Any], record: dict[str, Any], receipt: dict[str, Any]
    ) -> dict[str, Any]:
        del document["connections"][record["connection_id"]]
        document["catalogs"].pop(record["connection_id"], None)
        return self.service.requests.complete(
            receipt, {"connection_id": record["connection_id"], "deleted": True}
        )

    @staticmethod
    def _new(args: dict[str, Any]) -> dict[str, Any]:
        provider_id = args["provider_id"]
        endpoint = args.get("endpoint") or {
            "ollama": "http://127.0.0.1:11434",
            "codex-app-server": "https://chatgpt.com",
            "openai-api": "https://api.openai.com",
            "anthropic-api": "https://api.anthropic.com",
            "claude-agent-sdk": "https://api.anthropic.com",
        }.get(provider_id)
        if endpoint is None:
            raise InvalidArgumentError("This provider requires an explicit API base endpoint")
        auth_method = args.get("auth_method", AUTH_METHODS[provider_id])
        if auth_method not in SUPPORTED_AUTH_METHODS[provider_id]:
            raise InvalidArgumentError(
                "The authentication method is not supported by this provider"
            )
        api_surface, chat_max_tokens_field = Connections._compatible_options(
            provider_id,
            args.get("api_surface", "responses" if provider_id == "openai-api" else None),
            args.get("chat_max_tokens_field"),
        )
        endpoint = validate_endpoint(provider_id, endpoint)
        Connections._validate_compatible_auth(
            provider_id, auth_method, endpoint, args.get("api_key")
        )
        return {
            "connection_id": "conn-" + uuid.uuid4().hex,
            "revision": 1,
            "provider_id": provider_id,
            "display_name": args["display_name"],
            "enabled": True,
            "endpoint": endpoint,
            "auth_method": auth_method,
            "api_surface": api_surface,
            "chat_max_tokens_field": chat_max_tokens_field,
            "credential_present": False,
            "auth_state": "unverified" if auth_method == "none" else "unconfigured",
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
        provider_id = record["provider_id"]
        auth_method = args.get("auth_method", record["auth_method"])
        if auth_method not in SUPPORTED_AUTH_METHODS[provider_id]:
            raise InvalidArgumentError(
                "The authentication method is not supported by this provider"
            )
        if auth_method == "none" and args.get("api_key") is not None:
            raise InvalidArgumentError("This provider does not accept API credentials")
        if auth_method == "chatgpt" and "api_key" in args:
            raise InvalidArgumentError("This provider does not accept API credentials")
        endpoint = validate_endpoint(provider_id, args.get("endpoint", record["endpoint"]))
        api_surface, chat_max_tokens_field = Connections._compatible_options(
            provider_id,
            args.get("api_surface", record.get("api_surface")),
            args.get("chat_max_tokens_field", record.get("chat_max_tokens_field")),
        )
        Connections._validate_compatible_auth(
            provider_id, auth_method, endpoint, args.get("api_key")
        )
        if (
            provider_id == "openai-compatible-api"
            and auth_method == "none"
            and record.get("credential_present")
            and args.get("api_key", ...) is not None
        ):
            raise InvalidArgumentError(
                "Switching to unauthenticated access requires explicit credential deletion"
            )
        if "display_name" in args:
            if not args["display_name"].strip():
                raise InvalidArgumentError("A provider connection display name is required")
            record["display_name"] = args["display_name"]
        if endpoint != record["endpoint"]:
            if record["credential_present"] and "api_key" not in args:
                raise InvalidArgumentError("Endpoint changes require an explicit credential update")
            record["endpoint"] = endpoint
            invalidate_checks(document, record)
        if auth_method != record["auth_method"]:
            record["auth_method"] = auth_method
            invalidate_checks(document, record)
        if api_surface != record.get("api_surface") or chat_max_tokens_field != record.get(
            "chat_max_tokens_field"
        ):
            record["api_surface"] = api_surface
            record["chat_max_tokens_field"] = chat_max_tokens_field
            invalidate_checks(document, record)
        if "enabled" in args:
            reenabled = args["enabled"] and not record["enabled"]
            record["enabled"] = args["enabled"]
            if not record["enabled"]:
                cancel_auth(document, record, "connection_disabled")
            elif reenabled:
                invalidate_checks(document, record)

    @staticmethod
    def _compatible_options(
        provider_id: str, api_surface: Any, chat_max_tokens_field: Any
    ) -> tuple[str | None, str | None]:
        if provider_id == "openai-api":
            if api_surface not in (None, "responses") or chat_max_tokens_field is not None:
                raise InvalidArgumentError("Official OpenAI API uses the Responses API")
            return "responses", None
        if provider_id != "openai-compatible-api":
            if api_surface is not None or chat_max_tokens_field is not None:
                raise InvalidArgumentError("This provider does not accept OpenAI API settings")
            return None, None
        if api_surface not in {"responses", "chat_completions"}:
            raise InvalidArgumentError("Select an explicit OpenAI-compatible API surface")
        if api_surface == "responses":
            if chat_max_tokens_field is not None:
                raise InvalidArgumentError("Responses API does not use a chat token field")
            return api_surface, None
        if chat_max_tokens_field not in {"max_tokens", "max_completion_tokens"}:
            raise InvalidArgumentError("Select the compatible chat output-token field")
        return api_surface, chat_max_tokens_field

    @staticmethod
    def _validate_compatible_auth(
        provider_id: str, auth_method: str, endpoint: str, api_key: Any
    ) -> None:
        if provider_id != "openai-compatible-api":
            return
        if auth_method == "none":
            if api_key is not None:
                raise InvalidArgumentError("Unauthenticated connections do not accept an API key")
            host = urlsplit(endpoint).hostname
            try:
                address = ipaddress.ip_address(host or "")
            except ValueError:
                address = None
            if address is None or not address.is_loopback:
                raise InvalidArgumentError(
                    "Unauthenticated OpenAI-compatible APIs are restricted to numeric loopback"
                )

    def _replace_credential(
        self,
        document: dict[str, Any],
        record: dict[str, Any],
        receipt: dict[str, Any],
        value: str | None,
    ) -> None:
        service = self.service
        old_account = record.get("secret_account")
        pending_accounts = record.get("pending_secret_accounts", [])
        if not isinstance(pending_accounts, list):
            pending_accounts = []
        cleanup_accounts = {
            account
            for account in pending_accounts
            if service._is_provider_secret_account(record["connection_id"], account)
        }
        if service._is_provider_secret_account(record["connection_id"], old_account):
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

    @staticmethod
    def _reject_secret_reflection(value: Any, secrets: tuple[str, ...]) -> None:
        if not secrets:
            return
        try:
            check_public_payload(value, secrets=secrets, reject_credentials=False)
        except NarumiError:
            raise InvalidArgumentError(
                "Provider credentials cannot also be used as public metadata"
            ) from None
