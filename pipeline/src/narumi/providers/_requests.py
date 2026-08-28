"""Durable receipts; secret arguments are compared with a Keychain-backed HMAC."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
from typing import Any

from narumi.errors import ConfigurationConflictError, NarumiError
from narumi.providers._common import timestamp
from narumi.providers.secrets import SecretStore


class RequestLedger:
    def __init__(self, secret_store: SecretStore, namespace: str, server_instance_id: str) -> None:
        self._secrets = secret_store
        self._account = f"providers:{namespace}:request-hmac"
        self._server_instance_id = server_instance_id

    def fingerprint(self, tool: str, args: dict[str, Any]) -> dict[str, str]:
        encoded = json.dumps(
            {"tool": tool, "args": {k: v for k, v in args.items() if k != "request_id"}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if isinstance(args.get("api_key"), str):
            key = self._secrets.get(self._account)
            if key is None:
                key = secrets.token_hex(32)
                self._secrets.set(self._account, key)
            return {
                "scheme": "hmac-sha256",
                "digest": hmac.new(
                    key.encode(),
                    encoded,
                    hashlib.sha256,
                ).hexdigest(),
            }
        return {"scheme": "sha256", "digest": hashlib.sha256(encoded).hexdigest()}

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
    ) -> dict[str, Any]:
        receipt = {
            "fingerprint": fingerprint,
            "state": "pending",
            "response": response,
            "server_instance_id": self._server_instance_id,
            "created_at": timestamp(),
        }
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
