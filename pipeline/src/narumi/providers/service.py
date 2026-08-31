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
from narumi.errors import InvalidArgumentError, NarumiError
from narumi.providers._common import public_connection, timestamp
from narumi.providers._requests import RequestLedger
from narumi.providers.auth import Authentication
from narumi.providers.catalog import ModelCatalog
from narumi.providers.connections import Connections
from narumi.providers.metadata import MetadataClient
from narumi.providers.runtime import RuntimeInspector, RuntimePreparation
from narumi.providers.secrets import KeychainSecretStore, SecretStore
from narumi.providers.store import ProviderStore


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
        http_backend: Any | None = None,
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
        self._http_backend = http_backend
        self._http_lock = threading.Lock()
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

    def prepare_runtime(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.runtime.prepare(self.validate("prepare_provider_runtime", args))

    def observe_job(self, job_id: str, status: str) -> None:
        """Reconcile cancellations of queued jobs whose callable never got to run."""
        if self._can_recover:
            self.runtime.observe_job(job_id, status)

    def _recover(self) -> None:
        if not self._can_recover:
            return
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
        self.auth.codex.forget()
        try:
            self._interrupt_owned_operations()
        finally:
            try:
                with self._codex_lock:
                    backend = self._codex_backend
                if backend is not None:
                    backend.close()
            except Exception:
                raise NarumiError("Provider runtime shutdown could not be confirmed") from None
            finally:
                if self._owns_executor:
                    self.auth_executor.shutdown(wait=False, cancel_futures=True)
