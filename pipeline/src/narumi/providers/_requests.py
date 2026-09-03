"""Durable receipts; secret arguments are compared with a Keychain-backed HMAC."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
from typing import Any

from narumi.errors import (
    BusyError,
    ConfigurationConflictError,
    InvalidArgumentError,
    NarumiError,
)
from narumi.providers._common import timestamp
from narumi.providers.secrets import SecretStore


class RequestLedger:
    def __init__(self, secret_store: SecretStore, namespace: str, server_instance_id: str) -> None:
        self._secrets = secret_store
        self._namespace = namespace
        self._account = f"providers:{namespace}:request-hmac"
        self._server_instance_id = server_instance_id

    def fingerprint(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        document: dict[str, Any],
    ) -> dict[str, str]:
        self.reject_secret_identifiers(document, args)
        encoded = json.dumps(
            {"tool": tool, "args": {k: v for k, v in args.items() if k != "request_id"}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if isinstance(args.get("api_key"), str):
            return {
                "scheme": "hmac-sha256",
                "digest": hmac.new(
                    self._fingerprint_key(document),
                    encoded,
                    hashlib.sha256,
                ).hexdigest(),
            }
        return {"scheme": "sha256", "digest": hashlib.sha256(encoded).hexdigest()}

    def credential_fingerprint(
        self,
        credential: str | None,
        *,
        document: dict[str, Any],
    ) -> dict[str, str]:
        if credential is None:
            return {
                "scheme": "sha256",
                "digest": hashlib.sha256(b"provider-credential:none").hexdigest(),
            }
        return {
            "scheme": "hmac-sha256",
            "digest": hmac.new(
                self._fingerprint_key(document),
                b"provider-credential:v1\0" + credential.encode(),
                hashlib.sha256,
            ).hexdigest(),
        }

    def _fingerprint_key(self, document: dict[str, Any]) -> bytes:
        try:
            key = self._secrets.get(self._account)
            if key is None:
                if self._has_fingerprint_key_history(document):
                    raise BusyError(
                        "Provider credentials are temporarily unavailable",
                        details={"reason": "credential_unavailable"},
                    )
                key = secrets.token_hex(32)
                self._secrets.set(self._account, key)
            if not isinstance(key, str) or not key:
                raise ValueError("invalid request fingerprint key")
            self._bind_fingerprint_key(document, key, persist_marker=True)
            return key.encode()
        except BusyError:
            raise
        except Exception:
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            ) from None

    def audit_fingerprint_key(
        self,
        document: dict[str, Any],
        *,
        persist_marker: bool,
    ) -> None:
        """Require the established HMAC key without ever bootstrapping during recovery."""
        if not self._has_fingerprint_key_history(document):
            return
        try:
            key = self._secrets.get(self._account)
            if not isinstance(key, str) or not key:
                raise ValueError("missing request fingerprint key")
        except Exception:
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            ) from None
        self._bind_fingerprint_key(document, key, persist_marker=persist_marker)

    def _bind_fingerprint_key(
        self,
        document: dict[str, Any],
        key: str,
        *,
        persist_marker: bool,
    ) -> None:
        marker = document.get("request_hmac_generation")
        expected = {
            "scheme": "sha256",
            "digest": hashlib.sha256(key.encode()).hexdigest(),
        }
        if isinstance(marker, dict):
            digest = marker.get("digest")
            if (
                marker.get("scheme") != "sha256"
                or not isinstance(digest, str)
                or not hmac.compare_digest(digest, expected["digest"])
            ):
                raise BusyError(
                    "Provider credentials are temporarily unavailable",
                    details={"reason": "credential_unavailable"},
                )
            return
        if marker in (None, 1) and self._has_unsettled_hmac_semantic_receipt(document):
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            )
        if marker not in (None, 1) or not persist_marker:
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            )
        document["request_hmac_generation"] = expected

    @staticmethod
    def _has_unsettled_hmac_semantic_receipt(document: dict[str, Any]) -> bool:
        """Do not bind an unhashed legacy marker after an uncertain paid probe."""
        for receipt in document.get("requests", {}).values():
            if not isinstance(receipt, dict) or receipt.get("state") not in {
                "pending",
                "unknown",
            }:
                continue
            if "semantic_fingerprint" not in receipt:
                continue
            if receipt.get("credential_fingerprint_scheme") != "sha256":
                return True
        return False

    def _has_fingerprint_key_history(self, document: dict[str, Any]) -> bool:
        if document.get("request_hmac_generation") is not None:
            return True
        for connection_id, record in document.get("connections", {}).items():
            if not isinstance(record, dict):
                continue
            if record.get("auth_method") == "api_key" and record.get("credential_present") is True:
                return True
            accounts = [record.get("secret_account")]
            pending = record.get("pending_secret_accounts")
            if isinstance(pending, list):
                accounts.extend(pending)
            if any(
                self._is_provider_secret_account(connection_id, account) for account in accounts
            ):
                return True
        for receipt in document.get("requests", {}).values():
            if not isinstance(receipt, dict):
                continue
            fingerprint = receipt.get("fingerprint")
            if isinstance(fingerprint, dict) and fingerprint.get("scheme") == "hmac-sha256":
                return True
            if (
                isinstance(receipt.get("semantic_fingerprint"), dict)
                and receipt.get("credential_fingerprint_scheme") != "sha256"
            ):
                return True
        return False

    def reject_secret_identifiers(
        self,
        document: dict[str, Any],
        args: dict[str, Any],
    ) -> None:
        """Reject public lookup/idempotency IDs that contain a saved credential."""
        identifiers = tuple(
            value
            for field in ("request_id", "start_request_id")
            if isinstance((value := args.get(field)), str)
        )
        if not identifiers:
            return
        credentials = list(self.known_credentials(document))
        supplied = args.get("api_key")
        if isinstance(supplied, str):
            credentials.append(supplied)
        if any(
            credential in identifier
            for identifier in identifiers
            for credential in dict.fromkeys(credentials)
        ):
            raise InvalidArgumentError("Provider request identifiers cannot be API credentials")

    def known_credentials(
        self,
        document: dict[str, Any],
    ) -> tuple[str, ...]:
        records = document.get("connections", {})
        selected = tuple(
            (candidate_id, record)
            for candidate_id, record in records.items()
            if isinstance(record, dict)
        )
        accounts: list[str] = []
        for candidate_id, record in selected:
            candidates = [record.get("secret_account")]
            pending = record.get("pending_secret_accounts")
            if isinstance(pending, list):
                candidates.extend(pending)
            accounts.extend(
                account
                for account in candidates
                if self._is_provider_secret_account(candidate_id, account)
            )
        credentials: list[str] = []
        try:
            for account in dict.fromkeys(accounts):
                credential = self._secrets.get(account)
                if credential is None:
                    continue
                if not isinstance(credential, str) or not credential:
                    raise ValueError("invalid credential")
                credentials.append(credential)
        except Exception:
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            ) from None
        return tuple(dict.fromkeys(credentials))

    def _is_provider_secret_account(self, connection_id: str, account: Any) -> bool:
        prefix = f"providers:{self._namespace}:{connection_id}:"
        if not isinstance(account, str) or not account.startswith(prefix):
            return False
        suffix = account[len(prefix) :]
        return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)

    def replay(
        self,
        document: dict[str, Any],
        args: dict[str, Any],
        fingerprint: dict[str, str],
    ) -> dict[str, Any] | None:
        receipt = document["requests"].get(args["request_id"])
        if receipt is None:
            return None
        previous = receipt["fingerprint"]
        if previous["scheme"] != fingerprint["scheme"] or not hmac.compare_digest(
            previous["digest"],
            fingerprint["digest"],
        ):
            raise ConfigurationConflictError("The request ID was already used for another change")
        if receipt["state"] == "succeeded":
            return copy.deepcopy(receipt["response"])
        if receipt.get("response") is not None:
            return copy.deepcopy(receipt["response"])
        raise NarumiError(
            "The original provider change is unresolved; inspect its saved state before retrying",
        )

    def accept(
        self,
        document: dict[str, Any],
        args: dict[str, Any],
        fingerprint: dict[str, str],
        *,
        response: dict[str, Any] | None = None,
        semantic_fingerprint: dict[str, str] | None = None,
        credential_fingerprint_scheme: str | None = None,
    ) -> dict[str, Any]:
        receipt = {
            "fingerprint": fingerprint,
            "state": "pending",
            "response": response,
            "server_instance_id": self._server_instance_id,
            "created_at": timestamp(),
        }
        if semantic_fingerprint is not None:
            receipt["semantic_fingerprint"] = copy.deepcopy(semantic_fingerprint)
        if credential_fingerprint_scheme is not None:
            if credential_fingerprint_scheme not in {"sha256", "hmac-sha256"}:
                raise ValueError("invalid credential fingerprint scheme")
            receipt["credential_fingerprint_scheme"] = credential_fingerprint_scheme
        document["requests"][args["request_id"]] = receipt
        return receipt

    @staticmethod
    def complete(receipt: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        receipt.update(state="succeeded", response=copy.deepcopy(response))
        return copy.deepcopy(response)

    @staticmethod
    def fail(receipt: dict[str, Any]) -> None:
        receipt["state"] = "failed"


def safe_secret_error() -> NarumiError:
    return NarumiError("Provider credentials could not be updated securely")
