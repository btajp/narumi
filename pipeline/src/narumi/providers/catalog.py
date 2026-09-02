"""Explicit metadata checks and connection-scoped, paginated model observations."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from narumi.errors import (
    BusyError,
    ConfigurationConflictError,
    EngineUnavailableError,
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
from narumi.providers.metadata.openai_compatible import verification_source_fingerprint
from narumi.providers.metadata.validation import MODEL_ID, check_public_payload

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService

PAGE_SIZE = 100
VERIFIABLE_PROVIDERS = frozenset({"claude-agent-sdk", "openai-compatible-api"})


@dataclass(frozen=True)
class CheckResult:
    models: list[dict[str, Any]] | None
    reason: str | None
    authentication_failed: bool = False
    runtime_catalog_revision: str | None = None


class ModelCatalog:
    def __init__(self, service: ProviderService) -> None:
        self.service = service

    def test(
        self,
        args: dict[str, Any],
        *,
        preserve_unversioned_verification: bool = True,
    ) -> dict[str, Any]:
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
                "kind": "metadata",
            }
        result = self.fetch(snapshot)
        with service.store.transaction() as document:
            check = document["checks"].get(snapshot["provider_id"])
            owns_check = (
                check is not None
                and check["token"] == token
                and check["server_instance_id"] == service.server_instance_id
            )
            self.release_check(document, snapshot["provider_id"], token)
            record = connection(document, snapshot["connection_id"])
            if (
                service.closed.is_set()
                or not owns_check
                or not self.same_configuration(record, snapshot)
            ):
                service.store.commit(document)
                raise ConfigurationConflictError("Provider connection changed during verification")
            self.apply(
                document,
                record,
                result,
                preserve_unversioned_verification=preserve_unversioned_verification,
            )
            return {
                "connection": public_connection(record),
                "connected": result.models is not None,
                "reason": result.reason,
            }

    def verify_model(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run one explicitly confirmed, fixed prompt before making a model selectable."""
        service = self.service
        token = uuid.uuid4().hex
        with service.store.transaction() as document:
            fingerprint = service.requests.fingerprint(
                "verify_provider_model", args, document=document
            )
            replay = service.requests.replay(document, args, fingerprint)
            if replay is not None:
                return replay
            record = connection(document, args["connection_id"])
            check_revision(record, args["expected_revision"])
            if (
                record["provider_id"] not in VERIFIABLE_PROVIDERS
                or args.get("confirmation") != "send_test_prompt_and_may_charge"
            ):
                raise InvalidArgumentError("This provider model cannot use the generation probe")
            if not record["enabled"]:
                raise ConfigurationConflictError("The provider connection is disabled")
            check_provider_idle(document, record["provider_id"])
            runtime = service.runtime._current(record["provider_id"], document)
            cached = document["catalogs"].get(record["connection_id"], {})
            model = self._probe_candidate(record, runtime, cached, args["model_id"])
            expected_runtime = expected_runtime_evidence(service, record["provider_id"], runtime)
            credential = self._probe_credential(record)
            credential_fingerprint = service.requests.credential_fingerprint(
                credential,
                document=document,
            )
            semantic_fingerprint = _probe_semantic_fingerprint(
                record,
                runtime,
                model,
                expected_runtime=expected_runtime,
                credential_fingerprint=credential_fingerprint,
            )
            self._reject_duplicate_unknown_probe(
                document,
                args["request_id"],
                semantic_fingerprint,
            )
            receipt = service.requests.accept(
                document,
                args,
                fingerprint,
                semantic_fingerprint=semantic_fingerprint,
                credential_fingerprint_scheme=credential_fingerprint["scheme"],
            )
            verified = cached.get("verified_models", {}).get(args["model_id"])
            if (
                isinstance(verified, dict)
                and verified.get("fingerprint") == _model_verification_fingerprint(model)
                and verified.get("runtime_catalog_revision") == runtime["catalog_revision"]
            ):
                response = self._verification_response(
                    record, cached, model, verified["verified_at"]
                )
                service.contracts.validate_output("verify_provider_model", response)
                return service.requests.complete(receipt, response)
            snapshot = copy.deepcopy(record)
            probe_model = copy.deepcopy(model)
            runtime_revision = runtime["catalog_revision"]
            runtime_version = runtime["version"]
            catalog_id = cached.get("catalog_id")
            probe_source_fingerprint = _verification_source_fingerprint(
                snapshot["provider_id"], probe_model
            )
            document["checks"][record["provider_id"]] = {
                "token": token,
                "server_instance_id": service.server_instance_id,
                "connection_id": record["connection_id"],
                "kind": "model_verification",
            }
        try:
            result = self._run_model_probe(
                snapshot,
                args["model_id"],
                credential=credential,
                expected_runtime=expected_runtime,
            )
        except Exception as error:
            outcome_unknown = isinstance(error, NarumiError) and (
                error.details.get("outcome_unknown")
                or error.details.get("reason") == "provider_generation_outcome_unknown"
            )
            self._fail_probe(
                args["request_id"],
                snapshot["provider_id"],
                snapshot["connection_id"],
                token,
                outcome_unknown=outcome_unknown,
            )
            if outcome_unknown:
                raise EngineUnavailableError(
                    "The provider model verification outcome is unknown",
                    details={
                        "reason": "provider_generation_outcome_unknown",
                        "outcome_unknown": True,
                    },
                ) from None
            raise EngineUnavailableError(
                "The provider model could not be verified",
                details={"reason": "model_generation_verification_failed"},
            ) from None
        if expected_runtime is not None and not returned_runtime_matches(result, expected_runtime):
            self._fail_probe(
                args["request_id"],
                snapshot["provider_id"],
                snapshot["connection_id"],
                token,
                outcome_unknown=True,
            )
            raise EngineUnavailableError(
                "The provider model verification runtime evidence was rejected",
                details={
                    "reason": "provider_generation_outcome_unknown",
                    "outcome_unknown": True,
                },
            )
        if _verified_model_id(result) != args["model_id"]:
            self._fail_probe(
                args["request_id"],
                snapshot["provider_id"],
                snapshot["connection_id"],
                token,
            )
            raise EngineUnavailableError(
                "The provider returned another model during verification",
                details={"reason": "model_generation_verification_failed"},
            )
        try:
            promoted_model = self._promoted_probe_model(snapshot, probe_model, result)
        except Exception:
            self._fail_probe(
                args["request_id"],
                snapshot["provider_id"],
                snapshot["connection_id"],
                token,
            )
            raise EngineUnavailableError(
                "The provider model verification response was rejected",
                details={"reason": "model_generation_verification_failed"},
            ) from None
        with service.store.transaction() as document:
            check = document["checks"].get(snapshot["provider_id"])
            receipt = document["requests"].get(args["request_id"])
            owns_check = self._owns_probe_check(
                check,
                token=token,
                server_instance_id=service.server_instance_id,
                connection_id=snapshot["connection_id"],
            )
            owns_receipt = self._owns_pending_probe_receipt(
                receipt,
                server_instance_id=service.server_instance_id,
                fingerprint=fingerprint,
            )
            if service.closed.is_set() or not owns_check or not owns_receipt:
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                    outcome_unknown=True,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider model verification ownership was interrupted"
                )

            record = document["connections"].get(snapshot["connection_id"])
            if not isinstance(record, dict) or not self._same_probe_configuration(record, snapshot):
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider connection changed during model verification"
                )
            runtime = service.runtime._current(record["provider_id"], document)
            cached = document["catalogs"].get(record["connection_id"], {})
            if (
                runtime.get("catalog_revision") != runtime_revision
                or runtime.get("version") != runtime_version
                or runtime.get("state") != "ready"
            ):
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider runtime changed during model verification"
                )
            if cached.get("catalog_id") != catalog_id:
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider model catalog changed during verification"
                )
            try:
                model = self._probe_candidate(record, runtime, cached, args["model_id"])
            except InvalidArgumentError:
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider model catalog changed during verification"
                ) from None
            if (
                _verification_source_fingerprint(snapshot["provider_id"], model)
                != probe_source_fingerprint
            ):
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider model catalog changed during verification"
                )
            verified_at = timestamp()
            updated_catalog = copy.deepcopy(cached)
            models = updated_catalog.get("models", [])
            index = next(
                (
                    index
                    for index, item in enumerate(models)
                    if item.get("model_id") == args["model_id"]
                ),
                None,
            )
            if index is None:
                self._settle_probe(
                    document,
                    args["request_id"],
                    snapshot["provider_id"],
                    snapshot["connection_id"],
                    token,
                )
                service.store.commit(document)
                raise ConfigurationConflictError(
                    "Provider model catalog changed during verification"
                )
            promoted_model["fetched_at"] = model.get("fetched_at") or promoted_model["fetched_at"]
            models[index] = copy.deepcopy(promoted_model)
            model = models[index]
            updated_catalog.setdefault("verified_models", {})[args["model_id"]] = {
                "fingerprint": _model_verification_fingerprint(model),
                "source_fingerprint": probe_source_fingerprint,
                "descriptor": copy.deepcopy(model),
                "runtime_catalog_revision": runtime_revision,
                "verified_at": verified_at,
            }
            updated_catalog["catalog_id"] = uuid.uuid4().hex
            response = self._verification_response(record, updated_catalog, model, verified_at)
            service.contracts.validate_output("verify_provider_model", response)
            document["catalogs"][record["connection_id"]] = updated_catalog
            self._release_owned_probe_check(
                document,
                snapshot["provider_id"],
                snapshot["connection_id"],
                token,
            )
            return service.requests.complete(receipt, response)

    def _run_model_probe(
        self,
        snapshot: dict[str, Any],
        model_id: str,
        *,
        credential: str | None,
        expected_runtime: dict[str, str] | None,
    ) -> Any:
        service = self.service
        should_cancel = service.closed.is_set
        if snapshot["provider_id"] == "claude-agent-sdk":
            if credential is None:
                raise InvalidArgumentError("Claude Agent SDK requires an API key")
            return service.claude_backend.verify_model(
                snapshot["connection_id"],
                credential,
                model_id,
                expected_runtime=expected_runtime,
                should_cancel=should_cancel,
            )
        return service.openai_compatible_backend.verify_model(
            snapshot["endpoint"],
            credential,
            model_id,
            auth_method=snapshot["auth_method"],
            api_surface=snapshot.get("api_surface"),
            chat_max_tokens_field=snapshot.get("chat_max_tokens_field"),
            should_cancel=should_cancel,
        )

    def _probe_credential(self, record: dict[str, Any]) -> str | None:
        if record["auth_state"] != "authenticated":
            raise InvalidArgumentError("Test the provider connection before verifying a model")
        if record["auth_method"] != "api_key":
            return None
        account = record.get("secret_account")
        if not record["credential_present"] or not isinstance(account, str):
            raise InvalidArgumentError("The provider credential is unavailable")
        try:
            credential = self.service.secrets.get(account)
        except Exception:
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            ) from None
        if not isinstance(credential, str) or not credential:
            raise InvalidArgumentError("The provider credential is unavailable")
        return credential

    @staticmethod
    def _reject_duplicate_unknown_probe(
        document: dict[str, Any],
        request_id: str,
        semantic_fingerprint: dict[str, str],
    ) -> None:
        """Never authorize a second paid send while an equivalent outcome is unknown."""
        for previous_id, receipt in document["requests"].items():
            if previous_id == request_id or not isinstance(receipt, dict):
                continue
            previous = receipt.get("fingerprint")
            if receipt.get("state") != "unknown" or not isinstance(previous, dict):
                continue
            previous_semantic = receipt.get("semantic_fingerprint")
            if not isinstance(previous_semantic, dict):
                raise EngineUnavailableError(
                    "A legacy provider operation has an unresolved outcome",
                    details={
                        "reason": "provider_generation_outcome_unknown",
                        "outcome_unknown": True,
                    },
                )
            candidate = previous_semantic
            expected = semantic_fingerprint
            previous_scheme = candidate.get("scheme")
            previous_digest = candidate.get("digest")
            if (
                isinstance(previous_scheme, str)
                and isinstance(previous_digest, str)
                and previous_scheme == expected["scheme"]
                and hmac.compare_digest(previous_digest, expected["digest"])
            ):
                raise EngineUnavailableError(
                    "The equivalent provider model verification outcome is unknown",
                    details={
                        "reason": "provider_generation_outcome_unknown",
                        "outcome_unknown": True,
                    },
                )

    def _promoted_probe_model(
        self, snapshot: dict[str, Any], existing: dict[str, Any], result: Any
    ) -> dict[str, Any]:
        if snapshot["provider_id"] == "openai-compatible-api":
            if not isinstance(result, dict):
                raise ValueError("compatible probe did not return a descriptor")
            promoted = copy.deepcopy(result)
            if verification_source_fingerprint(promoted) != verification_source_fingerprint(
                existing
            ):
                raise ValueError("compatible probe changed the discovered model identity")
        else:
            promoted = copy.deepcopy(existing)
            promoted.update(availability="available", reason=None)
        if (
            promoted.get("model_id") != existing.get("model_id")
            or promoted.get("availability") != "available"
            or promoted.get("reason") is not None
            or promoted.get("source") != "provider_api"
            or promoted.get("billing", {}).get("kind") != "api"
            or "llm" not in promoted.get("roles", [])
            or "text" not in promoted.get("input_modalities", [])
            or "text" not in promoted.get("output_modalities", [])
        ):
            raise ValueError("probe descriptor is not selectable")
        validation_payload = {
            "connection_id": snapshot["connection_id"],
            "connection_revision": snapshot["revision"],
            "models": [promoted],
            "next_cursor": None,
            "catalog_state": "ready",
            "fetched_at": promoted.get("fetched_at"),
        }
        self.service.contracts.validate_output("list_provider_models", validation_payload)
        return promoted

    def _fail_probe(
        self,
        request_id: str,
        provider_id: str,
        connection_id: str,
        token: str,
        *,
        outcome_unknown: bool = False,
    ) -> None:
        with self.service.store.transaction() as document:
            self._settle_probe(
                document,
                request_id,
                provider_id,
                connection_id,
                token,
                outcome_unknown=outcome_unknown,
            )

    def _settle_probe(
        self,
        document: dict[str, Any],
        request_id: str,
        provider_id: str,
        connection_id: str,
        token: str,
        *,
        outcome_unknown: bool = False,
    ) -> None:
        self._release_owned_probe_check(document, provider_id, connection_id, token)
        receipt = document["requests"].get(request_id)
        if self._owns_pending_probe_receipt(
            receipt,
            server_instance_id=self.service.server_instance_id,
        ):
            if outcome_unknown:
                receipt["state"] = "unknown"
            else:
                self.service.requests.fail(receipt)

    def _release_owned_probe_check(
        self,
        document: dict[str, Any],
        provider_id: str,
        connection_id: str,
        token: str,
    ) -> None:
        check = document["checks"].get(provider_id)
        if self._owns_probe_check(
            check,
            token=token,
            server_instance_id=self.service.server_instance_id,
            connection_id=connection_id,
        ):
            del document["checks"][provider_id]

    @staticmethod
    def _owns_probe_check(
        check: Any,
        *,
        token: str,
        server_instance_id: str,
        connection_id: str,
    ) -> bool:
        return (
            isinstance(check, dict)
            and check.get("token") == token
            and check.get("server_instance_id") == server_instance_id
            and check.get("connection_id") == connection_id
            and check.get("kind") == "model_verification"
        )

    @staticmethod
    def _owns_pending_probe_receipt(
        receipt: Any,
        *,
        server_instance_id: str,
        fingerprint: dict[str, str] | None = None,
    ) -> bool:
        return (
            isinstance(receipt, dict)
            and receipt.get("state") == "pending"
            and receipt.get("server_instance_id") == server_instance_id
            and (fingerprint is None or receipt.get("fingerprint") == fingerprint)
        )

    @classmethod
    def _same_probe_configuration(cls, record: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        fields = (
            "provider_id",
            "endpoint",
            "auth_method",
            "api_surface",
            "chat_max_tokens_field",
            "credential_present",
            "auth_state",
        )
        return cls.same_configuration(record, snapshot) and all(
            record.get(field) == snapshot.get(field) for field in fields
        )

    @staticmethod
    def _probe_candidate(
        record: dict[str, Any], runtime: dict[str, Any], cached: dict[str, Any], model_id: Any
    ) -> dict[str, Any]:
        if (
            not isinstance(model_id, str)
            or MODEL_ID.fullmatch(model_id) is None
            or runtime.get("state") != "ready"
            or not runtime.get("version")
            or cached.get("connection_revision") != record["revision"]
            or cached.get("runtime_catalog_revision") != runtime.get("catalog_revision")
            or record.get("catalog_state") != "ready"
        ):
            raise InvalidArgumentError("Refresh the model catalog before verification")
        matches = [model for model in cached.get("models", []) if model.get("model_id") == model_id]
        if len(matches) != 1:
            raise InvalidArgumentError("The provider model is not in the current catalog")
        model = matches[0]
        if (
            model.get("availability") not in {"unverified", "available"}
            or model.get("source") != "provider_api"
            or model.get("billing", {}).get("kind") != "api"
        ):
            raise InvalidArgumentError("The provider model is not eligible for verification")
        if record["provider_id"] != "openai-compatible-api" and (
            "llm" not in model.get("roles", [])
            or "text" not in model.get("input_modalities", [])
            or "text" not in model.get("output_modalities", [])
        ):
            raise InvalidArgumentError("The provider model is not eligible for verification")
        return model

    @staticmethod
    def _verification_response(
        record: dict[str, Any], cached: dict[str, Any], model: dict[str, Any], verified_at: str
    ) -> dict[str, Any]:
        return {
            "connection_id": record["connection_id"],
            "connection_revision": record["revision"],
            "model": copy.deepcopy(model),
            "catalog_state": record["catalog_state"],
            "verified_at": verified_at,
        }

    def fetch(self, snapshot: dict[str, Any]) -> CheckResult:
        """No state mutation and no generation. Discard all untrusted exception contents."""
        service = self.service
        credential = None
        if snapshot["auth_method"] == "chatgpt" and not snapshot["credential_present"]:
            return CheckResult(None, "credential_required", True)
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
            runtime = service.runtime._current(snapshot["provider_id"], service.store.read())
            if snapshot["provider_id"] == "codex-app-server":
                if runtime["state"] != "ready":
                    return CheckResult(None, "runtime_preparation_required")
                models = service.codex_backend.list_models(snapshot["connection_id"])
            elif snapshot["provider_id"] == "openai-compatible-api":
                models = service.metadata.fetch_openai_compatible(
                    endpoint,
                    credential,
                    auth_method=snapshot["auth_method"],
                    api_surface=snapshot.get("api_surface"),
                )
            else:
                models = service.metadata.fetch(snapshot["provider_id"], endpoint, credential)
            try:
                check_public_payload(models, secrets=(credential,) if credential else ())
            except NarumiError:
                return CheckResult(None, "metadata_response_rejected")
            payload = {
                "connection_id": snapshot["connection_id"],
                "connection_revision": snapshot["revision"],
                "models": models,
                "next_cursor": None,
                "catalog_state": "ready",
                "fetched_at": timestamp(),
            }
            service.contracts.validate_output("list_provider_models", payload)
            if len({model["model_id"] for model in models}) != len(models):
                return CheckResult(None, "metadata_response_rejected")
            reason = {
                "openai-api": "model_list_verified_generation_unchecked",
                "openai-compatible-api": "model_generation_verification_required",
                "claude-agent-sdk": "model_generation_verification_required",
            }.get(snapshot["provider_id"])
            return CheckResult(
                copy.deepcopy(models),
                reason,
                runtime_catalog_revision=(
                    runtime["catalog_revision"] if runtime["state"] == "ready" else None
                ),
            )
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
        *,
        preserve_unversioned_verification: bool = True,
    ) -> None:
        checked_at = timestamp()
        record["checked_at"] = checked_at
        if result.models is not None:
            record.update(auth_state="authenticated", catalog_state="ready")
            previous = document["catalogs"].get(record["connection_id"], {})
            preserved: dict[str, dict[str, Any]] = {}
            if record["provider_id"] in VERIFIABLE_PROVIDERS:
                previous_verified = previous.get("verified_models", {})
                for model in result.models:
                    verified = previous_verified.get(model.get("model_id"))
                    if (
                        isinstance(verified, dict)
                        and (
                            preserve_unversioned_verification
                            or _has_immutable_verification_source(record["provider_id"], model)
                        )
                        and verified.get("source_fingerprint")
                        == _verification_source_fingerprint(record["provider_id"], model)
                        and verified.get("runtime_catalog_revision")
                        == result.runtime_catalog_revision
                    ):
                        descriptor = verified.get("descriptor")
                        if (
                            isinstance(descriptor, dict)
                            and verified.get("fingerprint")
                            == _model_verification_fingerprint(descriptor)
                            and _verification_source_fingerprint(record["provider_id"], descriptor)
                            == verified.get("source_fingerprint")
                        ):
                            promoted = copy.deepcopy(descriptor)
                            promoted["fetched_at"] = model.get("fetched_at")
                            model.clear()
                            model.update(promoted)
                            preserved[model["model_id"]] = copy.deepcopy(verified)
            document["catalogs"][record["connection_id"]] = {
                "models": copy.deepcopy(result.models),
                "fetched_at": checked_at,
                "connection_revision": record["revision"],
                "catalog_id": uuid.uuid4().hex,
                "runtime_catalog_revision": result.runtime_catalog_revision,
                "verified_models": preserved,
            }
            return
        record["auth_state"] = "failed" if result.authentication_failed else "unverified"
        if result.authentication_failed:
            if record["auth_method"] == "chatgpt":
                record["credential_present"] = False
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
                {"connection_id": record["connection_id"], "expected_revision": record["revision"]},
                preserve_unversioned_verification=False,
            )
        document = self.service.store.read()
        record = connection(document, args["connection_id"])
        cached = document["catalogs"].get(record["connection_id"], {})
        catalog_state = self._catalog_state(document, record, cached)
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
        if catalog_state != "ready":
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
            "catalog_state": catalog_state,
            "fetched_at": cached.get("fetched_at"),
        }

    def _catalog_state(
        self, document: dict[str, Any], record: dict[str, Any], cached: dict[str, Any]
    ) -> str:
        """Cache-only reads must not present observations from an outdated adapter as ready."""
        state = record["catalog_state"]
        if state != "ready":
            return state
        runtime = self.service.runtime._current(record["provider_id"], document)
        if (
            not record["enabled"]
            or runtime["state"] != "ready"
            or cached.get("runtime_catalog_revision") != runtime["catalog_revision"]
            or cached.get("connection_revision") != record["revision"]
        ):
            return "stale"
        return state

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


def _verified_model_id(result: Any) -> str | None:
    if isinstance(result, dict):
        value = result.get("model_id")
    else:
        value = getattr(result, "model_id", None)
    return value if isinstance(value, str) else None


def expected_runtime_evidence(
    service: ProviderService, provider_id: str, runtime: dict[str, Any]
) -> dict[str, str] | None:
    if provider_id != "claude-agent-sdk":
        return None
    resources = runtime.get("resources")
    if not isinstance(resources, list) or len(resources) != 1:
        raise EngineUnavailableError("Claude Agent SDK runtime evidence is unavailable")
    try:
        evidence = service.runtime.inspector.expected_runtime(provider_id, resources[0])
    except NarumiError:
        raise
    except Exception:
        raise EngineUnavailableError("Claude Agent SDK runtime evidence is unavailable") from None
    if not isinstance(evidence, dict):
        raise EngineUnavailableError("Claude Agent SDK runtime evidence is unavailable")
    return copy.deepcopy(evidence)


def returned_runtime_matches(result: Any, expected: dict[str, str]) -> bool:
    observed = getattr(result, "runtime_evidence", None)
    if not isinstance(observed, dict) or observed != expected:
        return False
    try:
        from narumi.providers.claude import runtime_fingerprint

        return runtime_fingerprint(observed) == runtime_fingerprint(expected)
    except (TypeError, ValueError):
        return False


def _probe_semantic_fingerprint(
    record: dict[str, Any],
    runtime: dict[str, Any],
    model: dict[str, Any],
    *,
    expected_runtime: dict[str, str] | None,
    credential_fingerprint: dict[str, str],
) -> dict[str, str]:
    """Bind paid-probe retries to execution meaning, excluding labels and CAS revision."""
    fields = (
        "provider_id",
        "endpoint",
        "auth_method",
        "api_surface",
        "chat_max_tokens_field",
    )
    payload = {
        "identity_version": "provider-model-probe-v1",
        "connection": {field: record.get(field) for field in fields},
        "runtime": {
            "version": runtime.get("version"),
            "catalog_revision": runtime.get("catalog_revision"),
            "expected": expected_runtime,
        },
        "credential": credential_fingerprint,
        "model_id": model.get("model_id"),
        "model": _model_verification_fingerprint(model),
        "discovery": _verification_source_fingerprint(record["provider_id"], model),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"scheme": "sha256", "digest": hashlib.sha256(encoded).hexdigest()}


def _model_verification_fingerprint(model: dict[str, Any]) -> str:
    fields = (
        "model_id",
        "resolved_revision",
        "source",
        "roles",
        "input_modalities",
        "output_modalities",
        "context_window",
        "max_output_tokens",
        "parameter_schema",
    )
    payload = {field: model.get(field) for field in fields}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _verification_source_fingerprint(provider_id: str, model: dict[str, Any]) -> str:
    if provider_id == "openai-compatible-api":
        return verification_source_fingerprint(model)
    return _model_verification_fingerprint(model)


def _has_immutable_verification_source(provider_id: str, model: dict[str, Any]) -> bool:
    if provider_id != "openai-compatible-api":
        return True
    revision = model.get("resolved_revision")
    return isinstance(revision, str) and bool(revision)


def model_verification_evidence(
    catalog: dict[str, Any], model: dict[str, Any], runtime_catalog_revision: str
) -> dict[str, Any] | None:
    verified = catalog.get("verified_models", {}).get(model.get("model_id"))
    if (
        not isinstance(verified, dict)
        or verified.get("fingerprint") != _model_verification_fingerprint(model)
        or verified.get("runtime_catalog_revision") != runtime_catalog_revision
        or not isinstance(verified.get("verified_at"), str)
    ):
        return None
    return copy.deepcopy(verified)
