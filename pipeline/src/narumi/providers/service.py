"""Connection, metadata and preparation service; no meeting generation is performed here."""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from narumi.contracts.loader import ContractSet, load_contracts
from narumi.errors import BusyError, InvalidArgumentError, NarumiError
from narumi.providers._common import invalidate_checks, public_connection, timestamp
from narumi.providers._requests import RequestLedger
from narumi.providers.auth import Authentication
from narumi.providers.catalog import ModelCatalog
from narumi.providers.connections import Connections
from narumi.providers.metadata import MetadataClient
from narumi.providers.runtime import RuntimeInspector, RuntimePreparation
from narumi.providers.secrets import KeychainSecretStore, SecretStore
from narumi.providers.store import REGISTRY_VERSION, ProviderStore


class ProviderService:
    """Public contract boundary with injectable external effects for deterministic tests.

    Construction and list operations never load ambient credentials or start SDKs. An
    executor submitted through ``submit_job`` must schedule asynchronously, not invoke the
    function inline: preparation waits until its public job receipt is durable.
    Only the resident data-root lease owner may use ``recover=True``. Temporary contexts
    must disable recovery so construction and shutdown cannot reconcile another owner.
    """

    def __init__(
        self,
        root: Path,
        *,
        secret_store: SecretStore | None = None,
        metadata_client: MetadataClient | None = None,
        server_instance_id: str | None = None,
        submit_job: Callable[[Callable[..., Mapping[str, Any]]], str] | None = None,
        contracts: ContractSet | None = None,
        auth_executor: Executor | None = None,
        runtime_inspector: RuntimeInspector | None = None,
        connection_referenced: Callable[[str], bool] | None = None,
        codex_backend: Any | None = None,
        claude_backend: Any | None = None,
        http_backend: Any | None = None,
        openai_compatible_backend: Any | None = None,
        audio_backend: Any | None = None,
        recover: bool = True,
    ) -> None:
        self.root = Path(root).expanduser()
        self.store = ProviderStore(self.root)
        self.secrets = secret_store if secret_store is not None else KeychainSecretStore()
        self.metadata = metadata_client if metadata_client is not None else MetadataClient()
        self.server_instance_id = server_instance_id or str(uuid.uuid4())
        self.namespace = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        self.requests = RequestLedger(self.secrets, self.namespace, self.server_instance_id)
        self.contracts = contracts if contracts is not None else load_contracts()
        self.closed = threading.Event()
        self._can_recover = recover
        self._codex_backend = codex_backend
        self._codex_lock = threading.Lock()
        self._claude_backend = claude_backend
        self._claude_lock = threading.Lock()
        self._http_backend = http_backend
        self._http_lock = threading.Lock()
        self._openai_compatible_backend = openai_compatible_backend
        self._openai_compatible_lock = threading.Lock()
        self._audio_backend = audio_backend
        self._audio_lock = threading.Lock()
        self.auth_executor = auth_executor or ThreadPoolExecutor(
            max_workers=3,
            thread_name_prefix="narumi-provider-auth",
        )
        self._owns_executor = auth_executor is None
        self.connection_referenced = connection_referenced or (lambda _connection_id: False)
        self.connections = Connections(self)
        self.catalog = ModelCatalog(self)
        self.auth = Authentication(self)
        self.runtime = RuntimePreparation(
            self,
            submit_job=submit_job,
            inspector=runtime_inspector or RuntimeInspector(),
        )
        self._recover()

    @property
    def codex_backend(self) -> Any:
        """Construct the isolated adapter lazily, without launching its subprocess."""
        with self._codex_lock:
            if self.closed.is_set():
                raise NarumiError("Provider service is closed")
            if self._codex_backend is None:
                from narumi.providers.codex import CodexBackend

                self._codex_backend = CodexBackend(self.root)
            return self._codex_backend

    def validate(self, tool: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
        if self.closed.is_set():
            raise NarumiError("Provider service is closed")
        try:
            self.contracts.validate_input(tool, args)
        except InvalidArgumentError:
            raise InvalidArgumentError("Provider operation arguments are invalid") from None
        return dict(args or {})

    @property
    def claude_backend(self) -> Any:
        """Construct the isolated Claude SDK worker without starting a generation."""
        with self._claude_lock:
            if self.closed.is_set():
                raise NarumiError("Provider service is closed")
            if self._claude_backend is None:
                from narumi.providers.claude import ClaudeSDKBackend

                self._claude_backend = ClaudeSDKBackend(self.root)
            return self._claude_backend

    @property
    def http_backend(self) -> Any:
        """Construct the direct HTTP adapter without reading credentials or sending data."""
        with self._http_lock:
            if self.closed.is_set():
                raise NarumiError("Provider service is closed")
            if self._http_backend is None:
                from narumi.providers.http_generation import HTTPMinutesBackend

                self._http_backend = HTTPMinutesBackend(metadata=self.metadata)
            return self._http_backend

    @property
    def openai_compatible_backend(self) -> Any:
        """Construct the explicit OpenAI-compatible adapter without sending data."""
        with self._openai_compatible_lock:
            if self.closed.is_set():
                raise NarumiError("Provider service is closed")
            if self._openai_compatible_backend is None:
                from narumi.providers.openai_compatible import OpenAICompatibleBackend

                self._openai_compatible_backend = OpenAICompatibleBackend()
            return self._openai_compatible_backend

    @property
    def audio_backend(self) -> Any:
        """Construct the audio adapter without reading credentials or sending audio."""
        with self._audio_lock:
            if self.closed.is_set():
                raise NarumiError("Provider service is closed")
            if self._audio_backend is None:
                from narumi.providers.audio_transcription import AudioTranscriptionBackend

                self._audio_backend = AudioTranscriptionBackend()
            return self._audio_backend

    def list_providers(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.validate("list_providers", args)
        return self.runtime.list_providers()

    def list_connections(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.validate("list_provider_connections", args)
        document = self.store.read()
        return {
            "connections": [
                public_connection(record) for record in document["connections"].values()
            ]
        }

    def set_connection(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.connections.set(self.validate("set_provider_connection", args))

    def delete_connection(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.connections.delete(self.validate("delete_provider_connection", args))

    def authenticate(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.auth.authenticate(self.validate("authenticate_provider_connection", args))

    def auth_status(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.auth.status(self.validate("get_provider_auth_status", args))

    def test_connection(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.catalog.test(self.validate("test_provider_connection", args))

    def list_models(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.catalog.list_models(self.validate("list_provider_models", args))

    def verify_model(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.catalog.verify_model(self.validate("verify_provider_model", args))

    def prepare_runtime(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.runtime.prepare(self.validate("prepare_provider_runtime", args))

    def observe_job(self, job_id: str, status: str) -> None:
        """Reconcile cancellations of queued jobs whose callable never got to run."""
        if self._can_recover:
            self.runtime.observe_job(job_id, status)

    def _recover(self) -> None:
        if self._can_recover:
            self._migrate_legacy_registry_schema()
            with self.store.transaction() as document:
                self.requests.audit_fingerprint_key(document, persist_marker=True)
            self._migrate_secret_reflections()
        else:
            self._reject_unmigrated_secret_reflections()
        if not self._can_recover:
            return
        self._recover_pending_credentials()
        self._recover_codex_artifacts()
        document = self.store.read()
        if not document["requests"] and not document["checks"] and not document["auth_operations"]:
            return
        with self.store.transaction() as document:
            for operation in document["auth_operations"].values():
                operation.update(authorization_url=None, user_code=None)
                if operation["state"] != "pending" or (
                    operation["server_instance_id"] == self.server_instance_id
                ):
                    continue
                operation.update(
                    state="unknown",
                    authorization_url=None,
                    user_code=None,
                    reason="authentication_operation_interrupted",
                    updated_at=timestamp(),
                )
                record = document["connections"].get(operation["connection_id"])
                if (
                    record is not None
                    and record["active_auth"] is not None
                    and (record["active_auth"]["operation_id"] == operation["operation_id"])
                ):
                    record["active_auth"]["state"] = "unknown"
                    record["auth_state"] = "unknown"
            for runtime in document["runtimes"].values():
                if runtime.get("server_instance_id") == self.server_instance_id:
                    continue
                active = runtime.get("active_setup")
                if active is not None and active["state"] in ("queued", "running"):
                    active["state"] = "unknown"
                    runtime["state"] = "unknown"
                if runtime.pop("pending_submission", None):
                    runtime["state"] = "unknown"
            for receipt in document["requests"].values():
                if receipt["state"] == "pending" and (
                    receipt["server_instance_id"] != self.server_instance_id
                ):
                    receipt["state"] = "unknown"
            for provider_id, check in list(document["checks"].items()):
                if check["server_instance_id"] != self.server_instance_id:
                    del document["checks"][provider_id]

    def _migrate_legacy_registry_schema(self) -> None:
        if self.store.read()["version"] == REGISTRY_VERSION:
            return
        with self.store.transaction() as document:
            if document["version"] == REGISTRY_VERSION:
                return
            # Registry v1 predates verify_provider_model, so its provider-operation
            # receipts cannot represent an in-flight paid model probe. Generation
            # checkpoints live in a separate ledger and are not migrated here.
            for section in ("requests", "auth_operations", "checks", "catalogs", "runtimes"):
                document[section].clear()
            document["request_hmac_generation"] = None
            for record in document["connections"].values():
                record["active_auth"] = None
                record["catalog_state"] = "unfetched"
                record["checked_at"] = None
                record["auth_state"] = (
                    "unverified"
                    if record.get("credential_present") or record.get("auth_method") == "none"
                    else "unconfigured"
                )
            document["version"] = REGISTRY_VERSION

    def _recover_codex_artifacts(self) -> None:
        """Remove registered Codex crash copies only for the resident server owner."""
        from narumi.providers.codex._session import recover_connection_artifacts

        snapshot = self.store.read()
        connection_ids = tuple(
            connection_id
            for connection_id, record in snapshot["connections"].items()
            if record.get("provider_id") == "codex-app-server"
            and record.get("connection_id") == connection_id
        )
        failed = []
        for connection_id in connection_ids:
            try:
                recover_connection_artifacts(self.root, connection_id)
            except Exception:
                failed.append(connection_id)
        if not failed:
            return
        with self.store.transaction() as document:
            for connection_id in failed:
                record = document["connections"].get(connection_id)
                if record is None or record.get("provider_id") != "codex-app-server":
                    continue
                record.update(
                    credential_present=True,
                    auth_state="unknown",
                    catalog_state="unfetched",
                    checked_at=None,
                )
                document["catalogs"].pop(connection_id, None)
                active = record.get("active_auth")
                if not isinstance(active, dict):
                    continue
                operation = document["auth_operations"].get(active.get("operation_id"))
                if operation is not None and operation.get("state") in ("pending", "unknown"):
                    operation.update(
                        state="unknown",
                        authorization_url=None,
                        user_code=None,
                        reason="authentication_operation_interrupted",
                        updated_at=timestamp(),
                    )
                    record["active_auth"] = self.auth._active(operation)

    def _migrate_secret_reflections(self) -> None:
        """Remove legacy registry fields that contain a current or pending credential."""
        snapshot = self.store.read()
        credentials = self.requests.known_credentials(snapshot)
        if not credentials or not _contains_credential(snapshot, credentials):
            return
        with self.store.transaction() as document:
            for connection_id, record in document["connections"].items():
                if _contains_credential(record.get("display_name"), credentials):
                    record["display_name"] = _safe_recovery_label(connection_id, credentials)
                if _contains_credential(record.get("endpoint"), credentials):
                    record["endpoint"] = _safe_recovery_endpoint(record.get("provider_id"))
                    record["enabled"] = False
                    invalidate_checks(document, record)
                if _contains_credential(record.get("active_auth"), credentials):
                    record["active_auth"] = None
                    record["auth_state"] = (
                        "unverified"
                        if record.get("credential_present") or record.get("auth_method") == "none"
                        else "unconfigured"
                    )

            for section in ("requests", "auth_operations", "checks", "catalogs", "runtimes"):
                values = document[section]
                for key, value in list(values.items()):
                    if _contains_credential(key, credentials) or _contains_credential(
                        value, credentials
                    ):
                        del values[key]

            operation_ids = set(document["auth_operations"])
            for record in document["connections"].values():
                active = record.get("active_auth")
                if isinstance(active, dict) and active.get("operation_id") not in operation_ids:
                    record["active_auth"] = None
                    record["auth_state"] = (
                        "unverified"
                        if record.get("credential_present") or record.get("auth_method") == "none"
                        else "unconfigured"
                    )
                if (
                    record["connection_id"] not in document["catalogs"]
                    and record.get("catalog_state") == "ready"
                ):
                    record["catalog_state"] = "unfetched"
                    record["checked_at"] = None

            if _contains_credential(document, credentials):
                raise BusyError(
                    "Provider credentials are temporarily unavailable",
                    details={"reason": "credential_unavailable"},
                )
        if _contains_credential(self.store.read(), credentials):
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            )

    def _reject_unmigrated_secret_reflections(self) -> None:
        document = self.store.read()
        if document["version"] != REGISTRY_VERSION:
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            )
        self.requests.audit_fingerprint_key(document, persist_marker=False)
        credentials = self.requests.known_credentials(document)
        if credentials and _contains_credential(document, credentials):
            raise BusyError(
                "Provider credentials are temporarily unavailable",
                details={"reason": "credential_unavailable"},
            )

    def _recover_pending_credentials(self) -> None:
        """Delete every unreachable Keychain account recorded before an interrupted swap."""
        document = self.store.read()
        pending = {
            connection_id: tuple(record["pending_secret_accounts"])
            for connection_id, record in document["connections"].items()
            if isinstance(record.get("pending_secret_accounts"), list)
            and record["pending_secret_accounts"]
        }
        if pending:
            with self.store.transaction() as current:
                for connection_id in pending:
                    record = current["connections"].get(connection_id)
                    if record is None or not record.get("pending_secret_accounts"):
                        continue
                    record.update(secret_account=None, credential_present=False)
                    invalidate_checks(current, record)
        for connection_id, accounts in pending.items():
            if not accounts or any(
                not self._is_provider_secret_account(connection_id, account) for account in accounts
            ):
                continue
            try:
                for account in accounts:
                    self.secrets.delete(account)
            except Exception:
                # Keep the durable account names as a fail-closed recovery marker. A later
                # resident restart or explicit credential replacement retries the cleanup.
                continue
            try:
                with self.store.transaction() as current:
                    record = current["connections"].get(connection_id)
                    if record is None:
                        continue
                    unresolved = record.get("pending_secret_accounts", [])
                    if any(account not in accounts for account in unresolved):
                        continue
                    record.update(
                        secret_account=None,
                        credential_present=False,
                        pending_secret_accounts=[],
                    )
                    invalidate_checks(current, record)
            except Exception:
                # Deleting an already-absent Keychain account is safe on explicit recovery.
                # Until metadata confirms it, retain fail-closed behavior on the next load.
                continue

    def _is_provider_secret_account(self, connection_id: str, account: Any) -> bool:
        prefix = f"providers:{self.namespace}:{connection_id}:"
        if not isinstance(account, str) or not account.startswith(prefix):
            return False
        suffix = account[len(prefix) :]
        return len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)

    def _interrupt_owned_operations(self) -> None:
        if not self._can_recover:
            return
        self.runtime.close()
        with self.store.transaction() as document:
            for operation in document["auth_operations"].values():
                if operation["state"] == "pending" and (
                    operation["server_instance_id"] == self.server_instance_id
                ):
                    operation.update(
                        state="unknown",
                        authorization_url=None,
                        user_code=None,
                        reason="authentication_operation_interrupted",
                        updated_at=timestamp(),
                    )
                    record = document["connections"].get(operation["connection_id"])
                    active = None if record is None else record["active_auth"]
                    if active is not None and active["operation_id"] == operation["operation_id"]:
                        active["state"] = "unknown"
                        record["auth_state"] = "unknown"

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        failed = False
        first_public_error: NarumiError | None = None

        def remember(error: Exception) -> None:
            nonlocal failed, first_public_error
            failed = True
            if first_public_error is None and isinstance(error, NarumiError):
                first_public_error = error

        try:
            self.auth.codex.forget()
        except Exception as error:
            remember(error)
        try:
            self._interrupt_owned_operations()
        except Exception as error:
            remember(error)
        backends = (
            (self._codex_lock, self._codex_backend),
            (self._claude_lock, self._claude_backend),
            (self._openai_compatible_lock, self._openai_compatible_backend),
            (self._http_lock, self._http_backend),
            (self._audio_lock, self._audio_backend),
        )
        for lock, backend in backends:
            try:
                with lock:
                    close = None if backend is None else getattr(backend, "close", None)
                if callable(close):
                    close()
            except Exception as error:
                remember(error)
        if self._owns_executor:
            try:
                self.auth_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as error:
                remember(error)
        if first_public_error is not None:
            raise first_public_error
        if failed:
            raise NarumiError("Provider runtime shutdown could not be confirmed") from None


def _contains_credential(value: Any, credentials: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(credential in value for credential in credentials)
    if isinstance(value, dict):
        return any(
            _contains_credential(key, credentials) or _contains_credential(child, credentials)
            for key, child in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_credential(child, credentials) for child in value)
    return False


def _safe_recovery_label(connection_id: str, credentials: tuple[str, ...]) -> str:
    digest = hashlib.sha256(connection_id.encode()).hexdigest()
    for candidate in (f"Recovered connection {digest[:12]}", f"Connection {digest}"):
        if not _contains_credential(candidate, credentials):
            return candidate
    raise BusyError(
        "Provider credentials are temporarily unavailable",
        details={"reason": "credential_unavailable"},
    )


def _safe_recovery_endpoint(provider_id: Any) -> str:
    from narumi.providers.metadata.endpoints import (
        ANTHROPIC_ENDPOINT,
        CODEX_ENDPOINT,
        OLLAMA_ENDPOINT,
        OPENAI_ENDPOINT,
    )

    endpoints = {
        "anthropic-api": ANTHROPIC_ENDPOINT,
        "claude-agent-sdk": ANTHROPIC_ENDPOINT,
        "codex-app-server": CODEX_ENDPOINT,
        "ollama": OLLAMA_ENDPOINT,
        "openai-api": OPENAI_ENDPOINT,
        "openai-compatible-api": "http://127.0.0.1:1",
    }
    try:
        return endpoints[provider_id]
    except (KeyError, TypeError):
        raise BusyError(
            "Provider credentials are temporarily unavailable",
            details={"reason": "credential_unavailable"},
        ) from None
