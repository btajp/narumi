"""Durable preparation receipts and fixed installed-runtime inspection jobs."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from narumi.errors import (
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.providers._common import AUTH_METHODS, PROVIDERS, check_provider_idle, public_runtime
from narumi.providers._runtime_lease import RuntimeLease
from narumi.providers.runtime_catalog import RuntimeInspector

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService

__all__ = ["RuntimeInspector", "RuntimePreparation"]

ACCEPTANCE_TIMEOUT = 30.0


class RuntimePreparation:
    def __init__(
        self,
        service: ProviderService,
        *,
        submit_job: Callable[[Callable[..., Mapping[str, Any]]], str] | None,
        inspector: RuntimeInspector,
    ) -> None:
        self.service = service
        self.inspector = inspector
        self.submit_job = submit_job
        self._leases: dict[str, RuntimeLease] = {}
        self._lock = threading.RLock()

    def _current(self, provider_id: str, document: dict[str, Any]) -> dict[str, Any]:
        resource = (
            self.service.codex_backend.resource()
            if provider_id == "codex-app-server"
            else self.inspector.resource(provider_id)
        )
        revision = self.inspector.catalog_revision(resource)
        saved = document["runtimes"].get(provider_id, {})
        state = saved.get("state", "not_prepared")
        if saved.get("catalog_revision") != revision and state == "ready":
            state = "not_prepared"
        return {
            **copy.deepcopy(saved),
            "state": state,
            "version": resource["version"],
            "catalog_revision": revision,
            "resources": [resource],
            "active_setup": copy.deepcopy(saved.get("active_setup")),
            "last_setup": copy.deepcopy(saved.get("last_setup")),
        }

    def list_providers(self) -> dict[str, Any]:
        document = self.service.store.read()
        providers = []
        for provider_id, display_name in PROVIDERS.items():
            runtime = self._current(provider_id, document)
            if runtime["state"] != "ready":
                availability = "not_prepared"
                reason = runtime.get("reason") or "runtime_preparation_required"
            elif provider_id == "claude-agent-sdk":
                availability, reason = "unverified", "sdk_execution_isolation_unverified"
            elif provider_id == "ollama":
                availability, reason = "unverified", "local_server_verification_required"
            else:
                availability, reason = "available", None
            providers.append(
                {
                    "provider_id": provider_id,
                    "display_name": display_name,
                    "roles": ["llm", "transcription"] if provider_id == "openai-api" else ["llm"],
                    "auth_methods": [AUTH_METHODS[provider_id]],
                    "availability": availability,
                    "reason": reason,
                    "runtime": public_runtime(runtime),
                }
            )
        return {"providers": providers}

    def prepare(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.service
        gate = threading.Event()
        accepted = threading.Event()
        lease: RuntimeLease | None = None
        job_id: str | None = None
        try:
            with service.store.transaction() as document:
                fingerprint = service.requests.fingerprint("prepare_provider_runtime", args)
                replay = service.requests.replay(document, args, fingerprint)
                if replay is not None:
                    return replay
                current = self._current(args["provider_id"], document)
                if args["expected_catalog_revision"] != current["catalog_revision"]:
                    raise ConfigurationConflictError("Provider runtime catalog changed; refresh it")
                resource = current["resources"][0]
                if args["resource_id"] != resource["resource_id"]:
                    raise InvalidArgumentError(
                        "Provider runtime resource is not in the approved catalog"
                    )
                check_provider_idle(document, args["provider_id"])
                if self.submit_job is None:
                    raise EngineUnavailableError(
                        "Provider preparation needs the resident job manager"
                    )
                lease = RuntimeLease(service.root, args["provider_id"])
                current.update(
                    state="preparing",
                    reason=None,
                    pending_submission=args["request_id"],
                    server_instance_id=service.server_instance_id,
                )
                document["runtimes"][args["provider_id"]] = current
                receipt = service.requests.accept(document, args, fingerprint)
                # Persist acceptance before the job can inspect or change runtime files.
                service.store.commit(document)

                def run(progress: Any) -> Mapping[str, Any]:
                    if not gate.wait(ACCEPTANCE_TIMEOUT) or not accepted.is_set():
                        try:
                            self._abort_submission(args, progress.job_id)
                        finally:
                            self._release_lease(progress.job_id)
                        raise EngineUnavailableError(
                            "Provider preparation acceptance was interrupted"
                        )
                    return self._run(args["provider_id"], resource, progress)

                try:
                    job_id = self.submit_job(run)
                except Exception:
                    current.update(state="failed", reason="runtime_job_submission_failed")
                    current.pop("pending_submission", None)
                    service.requests.fail(receipt)
                    service.store.commit(document)
                    raise EngineUnavailableError(
                        "Provider preparation could not be scheduled"
                    ) from None
                current["active_setup"] = {
                    "job_id": job_id,
                    "start_request_id": args["request_id"],
                    "resource_id": args["resource_id"],
                    "state": "queued",
                }
                current.pop("pending_submission", None)
                response = service.requests.complete(receipt, {"job_id": job_id})
                service.store.commit(document)
                with self._lock:
                    self._leases[job_id] = lease
                accepted.set()
            return response
        except Exception as error:
            if not accepted.is_set() and lease is not None:
                self._abort_submission(args, job_id)
            if isinstance(error, OSError | ValueError):
                raise EngineUnavailableError(
                    "Provider runtime files could not be prepared securely"
                ) from None
            raise
        finally:
            if not accepted.is_set() and lease is not None:
                lease.release()
            gate.set()

    def _abort_submission(self, args: dict[str, Any], job_id: str | None) -> None:
        """A gated callable has not run: remove only its own unresolved acceptance."""
        try:
            with self.service.store.transaction() as document:
                runtime = document["runtimes"].get(args["provider_id"], {})
                active = runtime.get("active_setup")
                owns_intent = runtime.get("pending_submission") == args["request_id"]
                owns_active = active is not None and (
                    active["start_request_id"] == args["request_id"]
                )
                if not (owns_intent or owns_active) or (
                    runtime.get("server_instance_id") != self.service.server_instance_id
                ):
                    return
                runtime.pop("pending_submission", None)
                runtime.update(
                    active_setup=None, state="failed", reason="runtime_acceptance_not_completed"
                )
                receipt = document["requests"][args["request_id"]]
                if job_id is not None:
                    runtime["last_setup"] = {
                        "job_id": job_id,
                        "start_request_id": args["request_id"],
                        "resource_id": args["resource_id"],
                        "state": "failed",
                    }
                    self.service.requests.complete(receipt, {"job_id": job_id})
                else:
                    self.service.requests.fail(receipt)
        except Exception:
            # The disabled gate prevents effects even when storage is still unavailable.
            # The callable tries cleanup again after the caller has released its lock.
            pass

    def _run(self, provider_id: str, resource: dict[str, Any], progress: Any) -> Mapping[str, Any]:
        service = self.service
        job_id = progress.job_id
        try:
            with service.store.transaction() as document:
                runtime = document["runtimes"].get(provider_id, {})
                active = runtime.get("active_setup")
                if (
                    active is None
                    or active["job_id"] != job_id
                    or service.closed.is_set()
                    or active["state"] != "queued"
                    or runtime.get("server_instance_id") != service.server_instance_id
                ):
                    raise CancelledError("Provider preparation was cancelled")
                active["state"] = "running"
            progress("verify_runtime_catalog", 0.1)
            if provider_id == "codex-app-server":
                service.codex_backend.prepare(resource, progress)
            else:
                self.inspector.prepare(service.root, provider_id, resource, progress)
            if progress.cancelled or service.closed.is_set():
                raise CancelledError("Provider preparation was cancelled")
            self._finish(provider_id, job_id, "succeeded", "ready", None)
            return {
                "provider_id": provider_id,
                "runtime_state": "ready",
                "resource_id": resource["resource_id"],
            }
        except CancelledError:
            self._finish(
                provider_id, job_id, "cancelled", "not_prepared", "runtime_preparation_cancelled"
            )
            raise CancelledError("Provider preparation was cancelled") from None
        except Exception:
            state = "not_prepared" if resource["version"] is None else "failed"
            reason = (
                "runtime_dependency_missing"
                if resource["version"] is None
                else "runtime_preparation_failed"
            )
            self._finish(provider_id, job_id, "failed", state, reason)
            raise EngineUnavailableError("Provider runtime preparation failed") from None
        finally:
            self._release_lease(job_id)

    def _finish(
        self,
        provider_id: str,
        job_id: str,
        operation_state: str,
        runtime_state: str,
        reason: str | None,
    ) -> None:
        with self.service.store.transaction() as document:
            runtime = document["runtimes"].get(provider_id, {})
            active = runtime.get("active_setup")
            if (
                active is not None
                and active["job_id"] == job_id
                and active["state"] in ("queued", "running")
                and runtime.get("server_instance_id") == self.service.server_instance_id
            ):
                active["state"] = operation_state
                runtime.update(
                    last_setup=copy.deepcopy(active),
                    active_setup=None,
                    state=runtime_state,
                    reason=reason,
                )
                if runtime_state == "ready":
                    for record in document["connections"].values():
                        cached = document["catalogs"].get(record["connection_id"])
                        if (
                            record["provider_id"] == provider_id
                            and cached is not None
                            and (
                                cached.get("runtime_catalog_revision")
                                != runtime["catalog_revision"]
                            )
                        ):
                            record["catalog_state"] = "stale"

    def observe_job(self, job_id: str, status: str) -> None:
        if status not in ("cancelled", "failed"):
            return
        document = self.service.store.read()
        for provider_id, runtime in document["runtimes"].items():
            active = runtime.get("active_setup")
            if (
                active is not None
                and active["job_id"] == job_id
                and (
                    active["state"] in ("queued", "running")
                    and runtime.get("server_instance_id") == self.service.server_instance_id
                )
            ):
                self._finish(
                    provider_id,
                    job_id,
                    status,
                    "not_prepared",
                    "runtime_preparation_cancelled"
                    if status == "cancelled"
                    else "runtime_preparation_failed",
                )
                self._release_lease(job_id)

    def _release_lease(self, job_id: str) -> None:
        with self._lock:
            lease = self._leases.pop(job_id, None)
        if lease is not None:
            lease.release()

    def close(self) -> None:
        cancelled = []
        with self.service.store.transaction() as document:
            for runtime in document["runtimes"].values():
                active = runtime.get("active_setup")
                if (
                    active is not None
                    and active["state"] == "queued"
                    and runtime.get("server_instance_id") == self.service.server_instance_id
                ):
                    active["state"] = "cancelled"
                    runtime.update(
                        last_setup=copy.deepcopy(active),
                        active_setup=None,
                        state="not_prepared",
                        reason="runtime_preparation_cancelled",
                    )
                    cancelled.append(active["job_id"])
        for job_id in cancelled:
            self._release_lease(job_id)
