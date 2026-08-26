"""``Bundle``: filesystem layout + manifest persistence + idempotent stage runner."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from narumi.bundle.hashing import sha256_file, sha256_params
from narumi.bundle.manifest import ArtifactRecord, Manifest, Producer
from narumi.errors import InvalidArgumentError, NotFoundError

MEETING_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

SUBDIRS = (
    "tracks",
    "preprocess",
    "transcripts",
    "diarization",
    "merged",
    "minutes",
    "context",
    "logs",
)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_meeting_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class StageResult:
    key: str
    path: Path
    record: ArtifactRecord
    skipped: bool


class Bundle:
    """One meeting's session bundle rooted at ``meetings/<meeting_id>/``."""

    def __init__(self, path: Path, manifest: Manifest) -> None:
        self.path = Path(path)
        self.manifest = manifest

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(
        cls,
        meetings_root: Path,
        *,
        meeting_name: str,
        meeting_id: str | None = None,
        engagement: str | None = None,
        scope: str | None = None,
        profile: str = "default",
        config: Any | None = None,
    ) -> Bundle:
        meeting_id = meeting_id or new_meeting_id()
        if not MEETING_ID_RE.match(meeting_id):
            raise InvalidArgumentError(f"invalid meeting_id: {meeting_id}")
        path = Path(meetings_root) / meeting_id
        if path.exists():
            raise InvalidArgumentError(f"bundle already exists: {path}")
        now = utc_now_iso()
        manifest = Manifest(
            meeting_id=meeting_id,
            meeting_name=meeting_name,
            engagement=engagement,
            scope=scope,
            profile=profile,
            created_at=now,
            updated_at=now,
        )
        if config is not None:
            manifest.config = config
        bundle = cls(path, manifest)
        for sub in SUBDIRS:
            (path / sub).mkdir(parents=True, exist_ok=True)
        bundle.save()
        return bundle

    @classmethod
    def open(cls, path: Path) -> Bundle:
        path = Path(path)
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            raise NotFoundError(f"bundle not found: {path}", details={"path": str(path)})
        manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        return cls(path, manifest)

    @classmethod
    def find(cls, meetings_root: Path, meeting_id: str) -> Bundle:
        if not MEETING_ID_RE.match(meeting_id):
            raise InvalidArgumentError(f"invalid meeting_id: {meeting_id}")
        return cls.open(Path(meetings_root) / meeting_id)

    @classmethod
    def iter_bundles(cls, meetings_root: Path):
        root = Path(meetings_root)
        if not root.exists():
            return
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "manifest.json").exists():
                try:
                    yield cls.open(child)
                except Exception:  # noqa: BLE001 - a broken bundle must not hide the others
                    continue

    # ------------------------------------------------------------------ paths
    @property
    def meeting_id(self) -> str:
        return self.manifest.meeting_id

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.json"

    def dir(self, name: str) -> Path:
        if name not in SUBDIRS:
            raise InvalidArgumentError(f"unknown bundle subdir: {name}")
        p = self.path / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def relpath(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.path.resolve()))

    def abspath(self, rel: str) -> Path:
        return self.path / rel

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        self.manifest.updated_at = utc_now_iso()
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            self.manifest.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
        )
        tmp.replace(self.manifest_path)

    def write_json(self, rel: str, model: BaseModel | dict[str, Any]) -> Path:
        target = self.abspath(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(model, BaseModel):
            text = model.model_dump_json(indent=2)
        else:
            text = json.dumps(model, ensure_ascii=False, indent=2)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(target)
        return target

    def read_json(self, rel: str) -> Any:
        target = self.abspath(rel)
        if not target.exists():
            raise NotFoundError(f"artifact missing: {rel}", details={"path": str(target)})
        return json.loads(target.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ artifacts
    def artifact(self, key: str) -> ArtifactRecord | None:
        return self.manifest.artifacts.get(key)

    def artifact_path(self, key: str) -> Path:
        rec = self.artifact(key)
        if rec is None:
            raise NotFoundError(f"artifact not recorded: {key}")
        p = self.abspath(rec.path)
        if not p.exists():
            raise NotFoundError(f"artifact file missing: {rec.path}", details={"key": key})
        return p

    def artifact_hash(self, key: str) -> str:
        rec = self.artifact(key)
        if rec is None:
            raise NotFoundError(f"artifact not recorded: {key}")
        return rec.sha256

    def track_hash(self, track: str) -> str:
        rec = self.manifest.recording.tracks.get(track)
        if rec is None or rec.sha256 is None:
            raise NotFoundError(f"track not available: {track}")
        return rec.sha256

    def run_stage(
        self,
        key: str,
        *,
        inputs: dict[str, str],
        params: dict[str, Any],
        producer: Producer | tuple[str, str],
        output: str,
        fn: Callable[[Path], None],
        force: bool = False,
    ) -> StageResult:
        """Run ``fn(output_path)`` unless an identical artifact already exists.

        ``inputs`` maps a label (usually an upstream artifact key) to its sha256; ``params`` are the
        stage parameters. The pair is what makes "same inputs → same version" hold.
        """
        if isinstance(producer, tuple):
            producer = Producer(name=producer[0], version=producer[1])
        params_hash = sha256_params(params)
        out_path = self.abspath(output)
        existing = self.artifact(key)
        if (
            not force
            and existing is not None
            and existing.inputs == inputs
            and existing.params_hash == params_hash
            and existing.path == output
            and out_path.exists()
        ):
            return StageResult(key=key, path=out_path, record=existing, skipped=True)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fn(out_path)
        if not out_path.exists():
            raise InvalidArgumentError(f"stage {key} produced no output at {output}")
        record = ArtifactRecord(
            path=output,
            sha256=sha256_file(out_path),
            inputs=dict(inputs),
            params=dict(params),
            params_hash=params_hash,
            producer=producer,
            created_at=utc_now_iso(),
        )
        self.manifest.artifacts[key] = record
        self.save()
        return StageResult(key=key, path=out_path, record=record, skipped=False)

    # ------------------------------------------------------------------ minutes versions
    def next_minutes_version(self) -> int:
        latest = self.manifest.latest_minutes_version
        return 1 if latest is None else latest + 1

    def minutes_dir(self, version: int) -> Path:
        p = self.dir("minutes") / f"v{version}"
        p.mkdir(parents=True, exist_ok=True)
        return p
