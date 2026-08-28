"""``ServerContext``: everything a tool handler needs, built once per process."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from narumi.bundle import Manifest
from narumi.catalog import Catalog
from narumi.config import catalog_path, data_root, meetings_root
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import BusyError, ErrorCode, NarumiError
from narumi.gaia.settings import GAIA_CONNECTION_FILE, GaiaConnectionStore
from narumi.profiles import PROFILES_FILE, ProfileStore

from narumi_server.handlers import HANDLERS, Handler
from narumi_server.idempotency import IdempotencyStore
from narumi_server.jobs import JobManager
from narumi_server.locks import MeetingLocks
from narumi_server.recording import RecordingController

if TYPE_CHECKING:
    from narumi.providers.metadata import MetadataClient
    from narumi.providers.secrets import SecretStore
    from narumi.providers.service import ProviderService

logger = logging.getLogger(__name__)

ENV_VALIDATE_OUTPUT = "NARUMI_VALIDATE_OUTPUT"
DEFAULT_ACTOR = "server"


@dataclass
class ServerContext:
    data_root: Path
    meetings_root: Path
    contracts: ContractSet
    catalog: Catalog
    jobs: JobManager
    recorder: RecordingController
    profiles: ProfileStore
    """Saved meeting profiles (``<NARUMI_HOME>/profiles.json``); see ``narumi.profiles``."""
    gaia: GaiaConnectionStore
    """Dedicated Gaia connection settings; its credential is never part of a profile."""
    providers: ProviderService | None = field(default=None, repr=False)
    provider_secret_store: SecretStore | None = field(default=None, repr=False)
    provider_metadata_client: MetadataClient | None = field(default=None, repr=False)
    provider_codex_backend: Any | None = field(default=None, repr=False)
    provider_http_backend: Any | None = field(default=None, repr=False)
    transports: list[str] = field(default_factory=list)
    validate_output: bool = False
    handlers: Mapping[str, Handler] = field(default_factory=lambda: dict(HANDLERS))
    actor: str = DEFAULT_ACTOR
    server_instance_id: str = field(default_factory=lambda: str(uuid4()))
    """Opaque lifetime identity; never persisted or reused after a context restart."""
    idempotency: IdempotencyStore = field(init=False, repr=False)
    locks: MeetingLocks = field(default_factory=MeetingLocks, repr=False)
    """Per-meeting write locks shared by jobs and tool handlers (see ``narumi_server.locks``)."""
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.idempotency = IdempotencyStore(self.catalog)
        from narumi.providers.service import ProviderService

        if self.providers is None:
            self.providers = ProviderService(
                self.data_root,
                secret_store=self.provider_secret_store,
                metadata_client=self.provider_metadata_client,
                codex_backend=self.provider_codex_backend,
                http_backend=self.provider_http_backend,
                recover="streamable-http" in self.transports,
                contracts=self.contracts,
                server_instance_id=self.server_instance_id,
                submit_job=lambda fn: self.jobs.submit("provider_setup", None, fn),
                connection_referenced=self._provider_connection_referenced,
            )
        elif isinstance(self.providers, ProviderService):
            previous = self.providers.connection_referenced
            self.providers.connection_referenced = lambda connection_id: (
                previous(connection_id) or self._provider_connection_referenced(connection_id)
            )

    def _provider_connection_referenced(self, connection_id: str) -> bool:
        """Read saved references while the provider transaction excludes new references.

        Profiles and manifests use atomic replacement. Do not take their write locks:
        a meeting job may already own one while it waits for the provider transaction.
        The catalog is only an index and cannot prove that an unindexed bundle is absent.
        """
        try:
            for profile in self.profiles.list():
                selected = profile.config.minutes_model
                if selected is not None and selected.connection_id == connection_id:
                    return True
            for folder in self.meetings_root.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    contents = (folder / "manifest.json").read_text(encoding="utf-8")
                except FileNotFoundError:
                    # A meeting may have been moved to trash after the directory listing.
                    continue
                manifest = Manifest.model_validate_json(contents)
                selected = manifest.config.minutes_model
                if selected is not None and selected.connection_id == connection_id:
                    return True
        except (OSError, ValueError, NarumiError):
            raise BusyError("Saved model references could not be verified") from None
        return False

    def close(self) -> None:
        """Finalize a running recording, stop jobs and close the catalog. Idempotent.

        A recording that is still running is stopped through the ``stop_recording`` handler
        (without a process job) so its tracks are finalized and hashed into the manifest; only
        when that fails is the recorder aborted (which still lets it finalize its files).

        The recording goes first: it is the one thing that cannot be redone, and waiting for a
        running job (a transcription can take minutes) would let narumi.app's stop timeout
        SIGKILL the server before the manifest is updated. Jobs are re-runnable (they are marked
        failed at shutdown and stages are idempotent), and a job never holds the lock of the
        meeting being recorded, so the order is safe.
        """
        if self._closed:
            return
        self._closed = True
        self.finalize_recording()
        try:
            self.providers.close()
        finally:
            try:
                self.jobs.shutdown(wait=True)
            finally:
                self.catalog.close()

    def finalize_recording(self) -> None:
        if not self.recorder.is_active:
            self.recorder.abort()  # reaps a leftover process, if any
            return
        meeting_id = self.recorder.active_meeting_id
        handler = self.handlers.get("stop_recording")
        try:
            if handler is None:
                raise NarumiError("no stop_recording handler", code=ErrorCode.INTERNAL)
            handler(self, {"auto_process": False})
            logger.info("finalized recording %s at shutdown", meeting_id)
        except Exception:  # noqa: BLE001 - shutdown must not leave the recorder running
            logger.exception("could not finalize recording %s at shutdown; aborting", meeting_id)
            self.recorder.abort()


def validate_output_from_env(environ: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(ENV_VALIDATE_OUTPUT, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_context(
    data_root_override: Path | None = None,
    *,
    recorder_path: Path | None = None,
    contracts_dir: Path | None = None,
    transports: Sequence[str] = (),
    validate_output: bool | None = None,
    max_workers: int = 1,
    recover_jobs: bool = False,
    handlers: Mapping[str, Handler] | None = None,
    provider_service: ProviderService | None = None,
    provider_secret_store: SecretStore | None = None,
    provider_metadata_client: MetadataClient | None = None,
    provider_codex_backend: Any | None = None,
    provider_http_backend: Any | None = None,
    server_instance_id: str | None = None,
) -> ServerContext:
    """Assemble a :class:`ServerContext` from settings and the environment.

    ``NARUMI_HOME`` (data root), ``NARUMI_RECORDER`` (recorder binary), ``NARUMI_CONTRACTS_DIR``
    (contract files) and ``NARUMI_VALIDATE_OUTPUT`` (=1 validates every tool result against its
    outputSchema) are honoured unless the explicit argument overrides them.

    ``recover_jobs=True`` is reserved for the server owner holding the exclusive data-root
    lease. Temporary contexts must leave it disabled, even when sharing that owner's catalog.
    """
    root = data_root(data_root_override)
    contracts = load_contracts(contracts_dir)
    catalog = Catalog(catalog_path(root))
    jobs = JobManager(catalog, max_workers=max_workers, recover=recover_jobs)
    recorder = RecordingController(recorder_path)
    ctx = ServerContext(
        data_root=root,
        meetings_root=meetings_root(root),
        contracts=contracts,
        catalog=catalog,
        jobs=jobs,
        recorder=recorder,
        profiles=ProfileStore(root / PROFILES_FILE),
        gaia=GaiaConnectionStore(root / GAIA_CONNECTION_FILE),
        providers=provider_service,
        provider_secret_store=provider_secret_store,
        provider_metadata_client=provider_metadata_client,
        provider_codex_backend=provider_codex_backend,
        provider_http_backend=provider_http_backend,
        server_instance_id=server_instance_id if server_instance_id is not None else str(uuid4()),
        transports=list(transports),
        validate_output=(
            validate_output_from_env() if validate_output is None else bool(validate_output)
        ),
        handlers=dict(HANDLERS if handlers is None else handlers),
    )
    logger.info(
        "narumi-server context: data_root=%s contracts=%s (%s) recorder=%s validate_output=%s",
        root,
        contracts.path,
        contracts.contract_version,
        recorder.recorder_path,
        ctx.validate_output,
    )
    return ctx
