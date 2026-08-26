"""Developer CLI ``narumi``.

This CLI is a *developer tool*: it calls the ``narumi`` library directly (bundle, catalog,
pipeline) from the same process. It is **not** the product surface. The UI = API parity rule
(AGENTS.md 絶対原則 3) applies to the app: the menu-bar app must go through the MCP server
(``narumi-server``) and its contract-defined tools, never through this CLI or the library.

Every command follows the same conventions:

* the data root comes from ``--data-root`` / ``$NARUMI_HOME`` (see :mod:`narumi.config`)
* the session bundle on disk is the source of truth; the catalog is only refreshed afterwards
* :class:`narumi.errors.NarumiError` is printed as its ``to_payload()`` JSON on stderr with
  exit code 2, so scripts can parse ``{"error": {"code", "message", "details?"}}``

Stage modules (catalog, pipeline, preprocess, engines) are imported lazily inside the commands so
that ``narumi --help`` and unrelated commands keep working while one module is broken.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from narumi import __version__
from narumi.bundle import Bundle, TrackRecord, new_meeting_id, sha256_file
from narumi.config import ENV_HOME, ENV_RECORDER, catalog_path, data_root, meetings_root, repo_root
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError
from narumi.models import ExternalSendPolicy, MeetingConfig

ERROR_EXIT_CODE = 2
TRACK_NAMES = ("mic", "system", "screen")
LIST_COLUMNS = (
    "meeting_id",
    "status",
    "started_at",
    "scope",
    "latest_minutes_version",
    "meeting_name",
)
REGISTRIES: tuple[tuple[str, str, str], ...] = (
    ("transcription engines", "narumi.transcribe", "available_engines"),
    ("diarization engines", "narumi.diarize", "available_engines"),
    ("llm providers", "narumi.llm", "available_providers"),
    ("exporters", "narumi.export", "list_exporters"),
)


@dataclasses.dataclass(frozen=True)
class CliState:
    """Per-invocation state shared by the group and its commands."""

    root: Path

    @property
    def meetings(self) -> Path:
        return meetings_root(self.root)

    @property
    def catalog_db(self) -> Path:
        return catalog_path(self.root)


# ---------------------------------------------------------------------------- output helpers
def _echo_json(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _warn(message: str) -> None:
    click.echo(f"warning: {message}", err=True)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp. Naive values are interpreted as local time."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidArgumentError(
            f"invalid ISO 8601 timestamp: {value}", details={"value": value}
        ) from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(UTC)


def _to_plain(value: Any) -> Any:
    """Turn dataclasses / pydantic models / sqlite rows into JSON-friendly data."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _to_plain(v) for k, v in dataclasses.asdict(value).items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if hasattr(value, "keys") and hasattr(value, "__getitem__"):  # sqlite3.Row and friends
        return {str(k): _to_plain(value[k]) for k in value.keys()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_to_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: _to_plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _display_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    name = getattr(item, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(item, Mapping) and isinstance(item.get("name"), str):
        return str(item["name"])
    return str(item)


def _render_table(rows: list[dict[str, Any]], columns: Iterable[str]) -> str:
    cols = list(columns)
    cells = [[_cell(row.get(c)) for c in cols] for row in rows]
    widths = [len(c) for c in cols]
    for line in cells:
        for i, text in enumerate(line):
            widths[i] = max(widths[i], len(text))
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(text.ljust(widths[i]) for i, text in enumerate(line)) for line in cells]
    return "\n".join([header, sep, *body])


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)


def _process_result_payload(result: Any) -> dict[str, Any]:
    """Serialize ``narumi.pipeline.ProcessResult`` (attribute access keeps fakes simple)."""
    return {
        "meeting_id": result.meeting_id,
        "minutes_version": result.minutes_version,
        "stages": list(result.stages),
        "skipped": list(result.skipped),
        "unresolved_speakers": list(getattr(result, "unresolved_speakers", [])),
    }


def _progress(stage: str, fraction: float) -> None:
    click.echo(f"[{fraction:>4.0%}] {stage}", err=True)


# ---------------------------------------------------------------------------- catalog helpers
def _load_catalog_class() -> type | None:
    """Import ``narumi.catalog.Catalog`` or return ``None`` (caller decides how to report)."""
    try:
        from narumi.catalog import Catalog
    except ImportError as exc:
        _warn(f"catalog unavailable ({exc}); run `narumi catalog rebuild` once it is fixed")
        return None
    return Catalog


def _close(obj: Any) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        closer()


def _catalog_upsert(state: CliState, bundle: Bundle, *, index_segments: bool = False) -> None:
    """Refresh the index for one bundle. The bundle is the source of truth; the index is derived.

    ``index_segments=True`` also re-indexes ``merged/merged.json`` for full-text search (after
    ``process`` / ``regenerate``, which are the only commands that change it).
    """
    catalog_cls = _load_catalog_class()
    if catalog_cls is None:
        return
    catalog = catalog_cls(state.catalog_db)
    try:
        catalog.upsert_meeting(bundle)
        if index_segments:
            catalog.index_segments(bundle)
    except NarumiError as exc:
        exc.details.setdefault("meeting_id", bundle.meeting_id)
        raise
    finally:
        _close(catalog)


@contextmanager
def _catalog_refresh(state: CliState, bundle: Bundle) -> Iterator[None]:
    """Refresh the index after a pipeline run, also when it raised (status ``failed`` on disk).

    ``merged/merged.json`` is only re-indexed after a successful run; a failed one leaves the
    previous index and just records the new status.
    """
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        _catalog_upsert(state, bundle, index_segments=succeeded)


def _require_catalog(state: CliState) -> Any:
    try:
        from narumi.catalog import Catalog
    except ImportError as exc:
        raise NarumiError(
            f"catalog unavailable: {exc}",
            code=ErrorCode.INTERNAL,
            details={"module": "narumi.catalog"},
        ) from exc
    return Catalog(state.catalog_db)


def _load_probe_duration() -> Callable[[Path], float | None] | None:
    try:
        from narumi.preprocess import probe_duration
    except ImportError as exc:
        _warn(f"narumi.preprocess unavailable ({exc}); track durations left unknown")
        return None
    return probe_duration


# ---------------------------------------------------------------------------- doctor helpers
def _tool_version(binary: str) -> str:
    """First ``<tool> -version`` line, reduced to the version token when it has the usual shape."""
    completed = subprocess.run(
        [binary, "-version"], capture_output=True, text=True, timeout=15, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"exit {completed.returncode}: {completed.stderr.strip()[:200]}")
    text = completed.stdout or completed.stderr
    first = text.splitlines()[0].strip() if text.strip() else ""
    parts = first.split()
    if len(parts) >= 3 and parts[1] == "version":
        return parts[2]
    return first or "unknown"


def _recorder_candidates() -> list[Path]:
    env = os.environ.get(ENV_RECORDER)
    if env:
        return [Path(env).expanduser()]
    root = repo_root()
    return [
        root / "app" / ".build" / "release" / "narumi-recorder",
        root / "app" / ".build" / "debug" / "narumi-recorder",
    ]


def _describe_registry(module_name: str, attr: str) -> str:
    try:
        module = importlib.import_module(module_name)
        items = getattr(module, attr)()
    except Exception as exc:  # doctor reports every failure instead of aborting the checkup
        return f"unavailable: {type(exc).__name__}: {exc}"
    names = [_display_name(item) for item in items]
    return ", ".join(names) if names else "(none)"


# ---------------------------------------------------------------------------- group
class NarumiGroup(click.Group):
    """Top-level group that turns :class:`NarumiError` into a JSON payload + exit code 2."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except NarumiError as exc:
            click.echo(json.dumps(exc.to_payload(), ensure_ascii=False), err=True)
            ctx.exit(ERROR_EXIT_CODE)


@click.group(cls=NarumiGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="narumi")
@click.option(
    "--data-root",
    "data_root_opt",
    type=click.Path(file_okay=False, path_type=Path),
    envvar=ENV_HOME,
    show_envvar=True,
    default=None,
    help="Data root with meetings/ and narumi.db (default: ~/Library/Application Support/narumi).",
)
@click.pass_context
def cli(ctx: click.Context, data_root_opt: Path | None) -> None:
    """narumi developer CLI: bundle import, processing, export and catalog maintenance."""
    ctx.obj = CliState(root=data_root(data_root_opt))


# ---------------------------------------------------------------------------- import-recording
@cli.command("import-recording")
@click.option("--name", "meeting_name", required=True, help="Meeting name.")
@click.option(
    "--mic",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Microphone track file.",
)
@click.option(
    "--system",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="System-audio track file.",
)
@click.option(
    "--screen",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Screen recording file.",
)
@click.option("--scope", default=None, help="Affiliation scope (confidentiality boundary).")
@click.option("--engagement", default=None, help="Engagement / project label.")
@click.option(
    "--started-at",
    default=None,
    help="Recording start (ISO 8601; naive values are local time). Default: now.",
)
@click.option(
    "--copy/--link",
    "copy_files",
    default=True,
    show_default=True,
    help="Copy the files into the bundle, or hardlink them (same filesystem only).",
)
@click.pass_obj
def import_recording(
    state: CliState,
    meeting_name: str,
    mic: Path | None,
    system: Path | None,
    screen: Path | None,
    scope: str | None,
    engagement: str | None,
    started_at: str | None,
    copy_files: bool,
) -> None:
    """Create a bundle (status "recorded") from existing recording files."""
    sources = {"mic": mic, "system": system, "screen": screen}
    if mic is None and system is None:
        raise InvalidArgumentError("at least one of --mic / --system is required")
    started = _parse_timestamp(started_at) if started_at else datetime.now(UTC)
    probe = _load_probe_duration()
    durations = {
        name: (probe(src) if probe is not None else None)
        for name, src in sources.items()
        if src is not None
    }

    bundle = Bundle.create(
        state.meetings,
        meeting_name=meeting_name,
        meeting_id=new_meeting_id(started),
        engagement=engagement,
        scope=scope,
    )
    try:
        tracks_dir = bundle.dir("tracks")
        for name, src in sources.items():
            if src is None:
                continue
            dest = tracks_dir / (f"{name}{src.suffix}" if src.suffix else name)
            _place_file(src, dest, copy=copy_files)
            bundle.manifest.recording.tracks[name] = TrackRecord(
                path=bundle.relpath(dest),
                sha256=sha256_file(dest),
                bytes=dest.stat().st_size,
                duration_sec=durations[name],
            )
        known = [d for d in durations.values() if d is not None]
        duration = max(known) if known else None
        rec = bundle.manifest.recording
        rec.started_at = _iso_utc(started)
        rec.duration_sec = duration
        rec.stopped_at = _iso_utc(started + timedelta(seconds=duration)) if duration else None
        rec.recorder = {
            "importer": "narumi-cli",
            "mode": "copy" if copy_files else "link",
            "sources": {n: str(p.resolve()) for n, p in sources.items() if p is not None},
        }
        bundle.manifest.status = "recorded"
        bundle.save()
    except BaseException:
        shutil.rmtree(bundle.path, ignore_errors=True)
        raise

    _catalog_upsert(state, bundle)
    click.echo(bundle.meeting_id)


def _place_file(src: Path, dest: Path, *, copy: bool) -> None:
    if copy:
        shutil.copy2(src, dest)
        return
    try:
        os.link(src, dest)
    except OSError as exc:
        raise InvalidArgumentError(
            f"cannot hardlink {src} into the bundle ({exc.strerror}); use --copy",
            details={"source": str(src), "errno": exc.errno},
        ) from exc


# ---------------------------------------------------------------------------- pipeline commands
@cli.command()
@click.argument("meeting_id")
@click.option("--force", is_flag=True, help="Re-run every stage even if inputs are unchanged.")
@click.pass_obj
def process(state: CliState, meeting_id: str, force: bool) -> None:
    """Run the full pipeline (preprocess → transcribe → diarize → align → integrate → generate)."""
    from narumi.pipeline import process_meeting

    bundle = Bundle.find(state.meetings, meeting_id)
    with _catalog_refresh(state, bundle):
        result = process_meeting(bundle, force=force, progress=_progress)
    _echo_json(_process_result_payload(result))


@cli.command()
@click.argument("meeting_id")
@click.option("--force", is_flag=True, help="Re-run alignment onward even if inputs are unchanged.")
@click.option("--reason", default="regenerate", show_default=True, help="Recorded in manifest.")
@click.pass_obj
def regenerate(state: CliState, meeting_id: str, force: bool, reason: str) -> None:
    """Re-run alignment → integrate → generate (never preprocess / transcribe)."""
    from narumi.pipeline import regenerate_meeting

    bundle = Bundle.find(state.meetings, meeting_id)
    with _catalog_refresh(state, bundle):
        result = regenerate_meeting(bundle, force=force, progress=_progress, reason=reason)
    _echo_json(_process_result_payload(result))


@cli.command()
@click.argument("meeting_id")
@click.option("--to", "destination", required=True, help="Exporter name (markdown, html, …).")
@click.option(
    "--path",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Output path passed to the exporter as options.output_path (made absolute).",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace an existing file at --path (options.overwrite).",
)
@click.option("--version", "minutes_version", type=int, default=None, help="Default: latest.")
@click.pass_obj
def export(
    state: CliState,
    meeting_id: str,
    destination: str,
    out_path: Path | None,
    overwrite: bool,
    minutes_version: int | None,
) -> None:
    """Export a minutes version through a registered exporter."""
    from narumi.pipeline import export_meeting

    bundle = Bundle.find(state.meetings, meeting_id)
    options: dict[str, Any] = {}
    if out_path is not None:
        options["output_path"] = str(out_path.expanduser().resolve())
    if overwrite:
        options["overwrite"] = True
    result = export_meeting(
        bundle, destination, options=options or None, minutes_version=minutes_version
    )
    _catalog_upsert(state, bundle)
    _echo_json(
        {
            "meeting_id": bundle.meeting_id,
            "destination": result.destination,
            "ref": result.ref,
            "minutes_version": result.minutes_version,
            "at": result.at,
            "details": _to_plain(getattr(result, "details", {})),
        }
    )


# ---------------------------------------------------------------------------- show / list
@cli.command()
@click.argument("meeting_id")
@click.pass_obj
def show(state: CliState, meeting_id: str) -> None:
    """Print a manifest summary as JSON."""
    bundle = Bundle.find(state.meetings, meeting_id)
    m = bundle.manifest
    _echo_json(
        {
            "meeting_id": m.meeting_id,
            "meeting_name": m.meeting_name,
            "engagement": m.engagement,
            "scope": m.scope,
            "profile": m.profile,
            "status": m.status,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
            "bundle_path": str(bundle.path),
            "recording": {
                "started_at": m.recording.started_at,
                "stopped_at": m.recording.stopped_at,
                "duration_sec": m.recording.duration_sec,
                "tracks": {k: v.model_dump(mode="json") for k, v in m.recording.tracks.items()},
            },
            "config": m.config.model_dump(mode="json"),
            "artifacts": sorted(m.artifacts),
            "contexts": [
                {"context_id": c.context_id, "source_type": c.source_type, "status": c.status}
                for c in m.contexts
            ],
            "minutes_versions": [v.model_dump(mode="json") for v in m.minutes_versions],
            "latest_minutes_version": m.latest_minutes_version,
            "exports": [
                e.model_dump(mode="json", exclude={"request_id"}, exclude_none=True)
                for e in m.exports
            ],
        }
    )


@cli.command("list")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="Scope name. Repeat for an explicit cross-scope query. Omitted = unscoped meetings only.",
)
@click.option("--query", default=None, help="Full-text query over names / transcripts.")
@click.option("--limit", type=click.IntRange(1), default=50, show_default=True)
@click.pass_obj
def list_meetings(state: CliState, scopes: tuple[str, ...], query: str | None, limit: int) -> None:
    """List meetings from the catalog as a table."""
    catalog = _require_catalog(state)
    scope: str | list[str] | None
    if not scopes:
        scope = None
    elif len(scopes) == 1:
        scope = scopes[0]
    else:
        scope = list(scopes)
    try:
        rows = catalog.list_meetings(query=query, scope=scope, limit=limit)
    finally:
        _close(catalog)
    plain = [_to_plain(row) for row in rows]
    dict_rows = [r if isinstance(r, dict) else {"meeting_id": r} for r in plain]
    if not dict_rows:
        click.echo("(no meetings)")
        return
    click.echo(_render_table(dict_rows, LIST_COLUMNS))


# ---------------------------------------------------------------------------- catalog
@cli.group()
def catalog() -> None:
    """Catalog (narumi.db) maintenance."""


@catalog.command()
@click.pass_obj
def rebuild(state: CliState) -> None:
    """Rebuild narumi.db from the bundles under meetings/."""
    cat = _require_catalog(state)
    try:
        stats = cat.rebuild(state.meetings)
    finally:
        _close(cat)
    _echo_json(_to_plain(stats))


# ---------------------------------------------------------------------------- doctor
@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check the local environment (ffmpeg, recorder, engines, data root)."""
    state: CliState = ctx.obj
    healthy = True

    meetings = state.meetings
    count = sum(1 for p in meetings.iterdir() if (p / "manifest.json").exists())
    click.echo(f"data root: {state.root} (meetings: {count}, catalog: {state.catalog_db.name})")

    for tool in ("ffmpeg", "ffprobe"):
        binary = shutil.which(tool)
        if binary is None:
            click.echo(f"{tool}: missing (install with `brew install ffmpeg`)")
            healthy = False
            continue
        try:
            click.echo(f"{tool}: {_tool_version(binary)} ({binary})")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            click.echo(f"{tool}: broken ({binary}): {exc}")
            healthy = False

    candidates = _recorder_candidates()
    found = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if found is not None:
        click.echo(f"recorder: ok ({found})")
    else:
        looked = ", ".join(str(p) for p in candidates)
        click.echo(f"recorder: missing (looked in: {looked}; set {ENV_RECORDER} or swift build)")

    for label, module_name, attr in REGISTRIES:
        click.echo(f"{label}: {_describe_registry(module_name, attr)}")

    if not healthy:
        click.echo("doctor: ffmpeg/ffprobe are required; fix the items above", err=True)
        ctx.exit(1)


# ---------------------------------------------------------------------------- config
@cli.command()
@click.argument("meeting_id")
@click.option("--transcription-engine", default=None, help="auto | fake | mlx-whisper | …")
@click.option("--diarization-engine", default=None, help="none | fake | pyannote")
@click.option("--llm-provider", default=None, help="none | fake | claude-agent-sdk | …")
@click.option(
    "--external-send-policy",
    type=click.Choice([p.value for p in ExternalSendPolicy]),
    default=None,
)
@click.option("--language", default=None, help="Transcription language, e.g. ja")
@click.option("--self-name", default=None, help='Name for the mic speaker ("" clears it).')
@click.option(
    "--vocab-hint",
    "vocab_hints",
    multiple=True,
    help="Vocabulary hint. Repeatable; when given, replaces the whole list.",
)
@click.pass_obj
def config(
    state: CliState,
    meeting_id: str,
    transcription_engine: str | None,
    diarization_engine: str | None,
    llm_provider: str | None,
    external_send_policy: str | None,
    language: str | None,
    self_name: str | None,
    vocab_hints: tuple[str, ...],
) -> None:
    """Show or update manifest.config (validated as MeetingConfig)."""
    bundle = Bundle.find(state.meetings, meeting_id)
    updates: dict[str, Any] = {}
    for key, value in (
        ("transcription_engine", transcription_engine),
        ("diarization_engine", diarization_engine),
        ("llm_provider", llm_provider),
        ("external_send_policy", external_send_policy),
        ("language", language),
    ):
        if value is not None:
            updates[key] = value
    if self_name is not None:
        updates["self_name"] = self_name or None
    if vocab_hints:
        updates["vocab_hints"] = list(vocab_hints)

    if updates:
        merged = {**bundle.manifest.config.model_dump(mode="json"), **updates}
        try:
            bundle.manifest.config = MeetingConfig.model_validate(merged)
        except ValidationError as exc:
            raise InvalidArgumentError(
                "invalid meeting config",
                details={"errors": json.loads(exc.json(include_url=False))},
            ) from exc
        bundle.save()
        _catalog_upsert(state, bundle)
    _echo_json(
        {
            "meeting_id": bundle.meeting_id,
            "updated": sorted(updates),
            "config": bundle.manifest.config.model_dump(mode="json"),
        }
    )


def main() -> None:
    """Console-script entry point (``narumi``)."""
    cli(prog_name="narumi")


if __name__ == "__main__":  # pragma: no cover
    main()
