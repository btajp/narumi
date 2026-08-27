"""Background jobs (``process`` / ``regenerate`` / ``export``) persisted in the catalog.

A job is a plain callable ``fn(progress) -> dict`` run on a :class:`ThreadPoolExecutor`;
``progress`` is a :class:`JobProgress` (callable ``(stage, fraction)`` that also exposes
``job_id``). State transitions ``queued → running → succeeded | failed`` are written to
``narumi.db`` so ``get_job_status`` can read them from any thread; jobs are volatile and never
rebuilt from bundles (design doc §9).
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from narumi.catalog import ACTIVE_JOB_STATUSES, Catalog
from narumi.errors import BusyError, CancelledError, ErrorCode, NarumiError, NotFoundError

logger = logging.getLogger(__name__)

JobFn = Callable[["JobProgress"], Mapping[str, Any]]

CANCEL_MESSAGE = "cancelled by cancel_job"


def _cancelled_payload() -> dict[str, str]:
    return {"code": str(ErrorCode.CANCELLED), "message": CANCEL_MESSAGE}


class JobProgress:
    """Progress sink handed to a job: ``progress("transcribe", 0.4)``.

    It doubles as the cooperative cancellation checkpoint: when the job's cancel flag is set
    (:meth:`JobManager.cancel`), the next call raises :class:`narumi.errors.CancelledError`
    instead of recording progress, which unwinds the pipeline between two stages.
    """

    __slots__ = ("_cancel", "_catalog", "job_id")

    def __init__(
        self, catalog: Catalog, job_id: str, cancel_event: threading.Event | None = None
    ) -> None:
        self._catalog = catalog
        self.job_id = job_id
        self._cancel = cancel_event

    @property
    def cancelled(self) -> bool:
        """True once :meth:`JobManager.cancel` flagged this job (checked at each call)."""
        return self._cancel is not None and self._cancel.is_set()

    def __call__(self, stage: str, fraction: float) -> None:
        if self.cancelled:
            raise CancelledError(
                CANCEL_MESSAGE, details={"job_id": self.job_id, "stage": str(stage)}
            )
        try:
            clamped = min(1.0, max(0.0, float(fraction)))
        except (TypeError, ValueError):
            clamped = 0.0
        try:
            self._catalog.update_job(
                self.job_id, progress={"stage": str(stage), "fraction": clamped}
            )
        except NarumiError as exc:  # a vanished job row must not kill the pipeline
            logger.warning("progress update for %s failed: %s", self.job_id, exc)


class JobManager:
    """Thread-pool job runner with catalog persistence (default one worker: 1 会議ずつ処理)."""

    def __init__(self, catalog: Catalog, *, max_workers: int = 1) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.catalog = catalog
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="narumi-job"
        )
        # Reentrant: Future.cancel() runs the done-callback (_forget) synchronously in the
        # cancelling thread, which takes this lock again inside JobManager.cancel.
        self._lock = threading.RLock()
        self._futures: dict[str, Future[dict[str, Any]]] = {}
        self._meetings: dict[str, str | None] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._closed = False
        stale = catalog.mark_stale_jobs("job abandoned: narumi-server restarted")
        if stale:
            logger.warning("marked %d stale job(s) as failed at start-up", stale)

    # ------------------------------------------------------------------ submission
    def submit(self, kind: str, meeting_id: str | None, fn: JobFn) -> str:
        """Persist a ``queued`` job and schedule ``fn``. Returns the job id immediately.

        One meeting has at most one queued / running job: a second submission for the same
        ``meeting_id`` is ``busy``. The check happens under the manager's lock, so two handlers
        that both passed their own busy check cannot enqueue twice (check-then-act race).
        """
        with self._lock:
            if self._closed:
                raise NarumiError(
                    "job manager is shut down", code=ErrorCode.INTERNAL, details={"kind": kind}
                )
            if meeting_id is not None:
                active = [
                    job_id
                    for job_id, future in self._futures.items()
                    if not future.done() and self._meetings[job_id] == meeting_id
                ]
                if active:
                    raise BusyError(
                        "a job is already running for this meeting",
                        details={"meeting_id": meeting_id, "jobs": active},
                    )
            job_id = self.catalog.create_job(kind, meeting_id)
            self._cancel_events[job_id] = threading.Event()  # before _run can possibly start
            future = self._executor.submit(self._run, job_id, fn)
            self._futures[job_id] = future
            self._meetings[job_id] = meeting_id
        future.add_done_callback(lambda _f, job_id=job_id: self._forget(job_id))
        logger.info("job %s queued (%s, meeting %s)", job_id, kind, meeting_id)
        return job_id

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)
            self._meetings.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    def _run(self, job_id: str, fn: JobFn) -> dict[str, Any]:
        self.catalog.update_job(job_id, status="running")
        with self._lock:
            cancel_event = self._cancel_events.get(job_id)
        progress = JobProgress(self.catalog, job_id, cancel_event)
        try:
            result = fn(progress)
            if not isinstance(result, Mapping):
                raise NarumiError(
                    f"job returned {type(result).__name__}, expected a JSON object",
                    code=ErrorCode.INTERNAL,
                    details={"job_id": job_id},
                )
            payload = dict(result)
        except CancelledError as exc:
            logger.info("job %s cancelled: %s", job_id, exc.message)
            self.catalog.update_job(job_id, status="cancelled", error=exc.to_payload()["error"])
            raise
        except NarumiError as exc:
            logger.warning("job %s failed: %s: %s", job_id, exc.code, exc.message)
            self.catalog.update_job(job_id, status="failed", error=exc.to_payload()["error"])
            raise
        except Exception as exc:
            logger.error("job %s crashed:\n%s", job_id, traceback.format_exc())
            self.catalog.update_job(
                job_id,
                status="failed",
                error={
                    "code": str(ErrorCode.INTERNAL),
                    "message": f"{type(exc).__name__}: {exc}",
                    "details": {"exception": type(exc).__name__},
                },
            )
            raise
        self.catalog.update_job(job_id, status="succeeded", result=payload)
        logger.info("job %s succeeded", job_id)
        return payload

    # ------------------------------------------------------------------ cancellation
    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a job and return its catalog row right after the request.

        A queued job is cancelled immediately (status ``cancelled``, error code ``cancelled``).
        A running job is cancelled cooperatively: its cancel flag is set and the job's next
        progress checkpoint raises :class:`narumi.errors.CancelledError` — the returned row may
        therefore still say ``running``. Cancelling an already-cancelled job is a no-op.
        ``NotFoundError`` for unknown ids; ``BusyError`` when the job already finished
        (``succeeded`` / ``failed``) and can no longer be cancelled.
        """
        with self._lock:
            job = self.catalog.get_job(job_id)
            if job is None:
                raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})
            status = job["status"]
            if status == "cancelled":
                return job
            if status in ("succeeded", "failed"):
                raise BusyError(
                    f"job {job_id} already finished ({status}) and can no longer be cancelled",
                    details={"job_id": job_id, "status": status},
                )
            future = self._futures.get(job_id)
            event = self._cancel_events.get(job_id)
            if future is not None and future.cancel():
                # never started; the executor drops it and the done-callback forgets it
                self.catalog.update_job(job_id, status="cancelled", error=_cancelled_payload())
                logger.info("job %s cancelled while queued", job_id)
            elif event is not None:
                event.set()
                logger.info("job %s flagged for cooperative cancellation", job_id)
            else:
                # active in the catalog but unknown to this manager (normally impossible:
                # start-up marks such rows failed); close it out instead of leaving it stuck
                logger.warning("cancelling job %s that this manager does not own", job_id)
                self.catalog.update_job(job_id, status="cancelled", error=_cancelled_payload())
        refreshed = self.catalog.get_job(job_id)
        return job if refreshed is None else refreshed

    # ------------------------------------------------------------------ queries
    def active_jobs(self, meeting_id: str | None = None) -> list[str]:
        """Job ids that are queued or running (in this process, cross-checked with the catalog)."""
        with self._lock:
            return [
                job_id
                for job_id, future in self._futures.items()
                if not future.done()
                and (meeting_id is None or self._meetings[job_id] == meeting_id)
            ]

    def has_active(self, meeting_id: str) -> bool:
        if self.active_jobs(meeting_id):
            return True
        rows = self.catalog.list_jobs(meeting_id=meeting_id, statuses=ACTIVE_JOB_STATUSES, limit=1)
        return bool(rows)

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until the job finished; returns the catalog row. For tests and the CLI."""
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            try:
                future.result(timeout=timeout)
            except TimeoutError:
                raise
            except FutureCancelledError:  # BaseException: cancelled while still queued
                pass
            except Exception:  # the failure is recorded in the catalog; callers read it there
                pass
        job = self.catalog.get_job(job_id)
        if job is None:
            raise NarumiError(
                f"job not found: {job_id}", code=ErrorCode.NOT_FOUND, details={"job_id": job_id}
            )
        return job

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
        stale = self.catalog.mark_stale_jobs("job cancelled: narumi-server shut down")
        if stale:
            logger.info("marked %d unfinished job(s) as failed at shutdown", stale)
