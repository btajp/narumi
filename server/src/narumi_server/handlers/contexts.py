"""``register_context``: store an external source verbatim, then try to parse a transcript.

The raw payload saved under ``context/sources/`` stays the source of truth. When ``source_type``
is a transcript kind and the payload is text, the deterministic parsers
(:mod:`narumi.context_sources`: WebVTT / SRT / Zoom txt / plain) run immediately; success writes
the ``transcripts/ext-<context_id>`` artifact (keyed to the stored source's hash, 絶対原則 2)
and the context reports ``parsed``. Anything unparseable simply stays ``stored`` — never an
error. ``url`` payloads are stored as references only: fetching a URL would send the request
out of the machine, so it is left to a later, policy-checked step (design doc §11), and such
contexts always report ``stored``.
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi.bundle import Bundle, ContextRecord, utc_now_iso
from narumi.bundle.hashing import sha256_file
from narumi.context_sources import PARSER_VERSION, parse_context
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.models import Transcript

from narumi_server.handlers.common import locked_bundle, sync_catalog
from narumi_server.handlers.processing import enqueue_regenerate

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

SOURCES_DIR = "context/sources"
PAYLOAD_KEYS: tuple[str, ...] = ("content", "url", "file_path")
MAX_FILE_BYTES = 16 * 1024 * 1024
"""Upper bound for ``file_path`` payloads: they are read into memory, base64-encoded when binary
and embedded in a JSON document inside the bundle (and later in LLM prompts)."""


def new_context_id() -> str:
    return f"ctx-{secrets.token_hex(6)}"


def register_context(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    given = [key for key in PAYLOAD_KEYS if key in args]
    if len(given) != 1:
        raise InvalidArgumentError(
            "exactly one of content / url / file_path is required", details={"given": given}
        )
    kind = given[0]
    auto_regenerate = bool(args.get("auto_regenerate", False))
    # Every check that can fail (scope, busy, the file itself) runs before anything is written:
    # a rejected call leaves no context behind, so a retry with the same request_id is a clean
    # first execution rather than a duplicate.
    with locked_bundle(
        ctx,
        args["meeting_id"],
        scope=args.get("scope"),
        purpose="register_context",
        allow_recording=not auto_regenerate,
    ) as bundle:
        context_id = new_context_id()
        registered_at = utc_now_iso()
        source: dict[str, Any] = {
            "context_id": context_id,
            "meeting_id": bundle.meeting_id,
            "source_type": args["source_type"],
            "label": args.get("label"),
            "registered_at": registered_at,
            "request_id": args.get("request_id"),
        }
        if kind == "content":
            source["content"] = args["content"]
        elif kind == "url":
            source["url"] = args["url"]
        else:
            source.update(read_context_file(args["file_path"]))

        rel = f"{SOURCES_DIR}/{context_id}.json"
        bundle.write_json(rel, source)
        status = _try_parse(bundle, rel, source, context_id=context_id, kind=kind)
        bundle.manifest.contexts.append(
            ContextRecord(
                context_id=context_id,
                source_type=args["source_type"],
                registered_at=registered_at,
                path=rel,
                status=status,
                label=args.get("label"),
                request_id=args.get("request_id"),
            )
        )
        bundle.save()
        sync_catalog(ctx, bundle)
        ctx.catalog.record_context(
            bundle.meeting_id, context_id, args["source_type"], status, registered_at
        )
        ctx.catalog.audit(
            ctx.actor,
            "register_context",
            {"meeting_id": bundle.meeting_id, "context_id": context_id, "kind": kind},
        )

        result: dict[str, Any] = {"context_id": context_id, "status": status}
        if auto_regenerate:
            result["job_id"] = enqueue_regenerate(
                ctx, bundle.meeting_id, force=False, reason=f"register_context {context_id}"
            )
    return result


def _try_parse(
    bundle: Bundle, rel: str, source: dict[str, Any], *, context_id: str, kind: str
) -> str:
    """Parse the stored source into ``transcripts/ext-<context_id>`` when possible.

    Returns the context status: ``parsed`` when a transcript artifact was written, ``stored``
    otherwise (url references are never fetched here; binary payloads and non-transcript
    source types have nothing to parse — none of that is an error).
    """
    if kind == "url" or source.get("content_encoding") is not None:
        return "stored"
    text = source.get("content")
    if not isinstance(text, str):
        return "stored"
    transcript = parse_context(source["source_type"], text, context_id=context_id)
    if transcript is None:
        return "stored"
    _record_ext_transcript(bundle, rel, transcript, context_id=context_id)
    return "parsed"


def _record_ext_transcript(
    bundle: Bundle, source_rel: str, transcript: Transcript, *, context_id: str
) -> None:
    """Write the parsed transcript as an idempotent stage keyed to the stored source's hash."""
    output = f"transcripts/ext-{context_id}.json"
    fmt = transcript.engine.name.removeprefix("parser-")

    def produce(out: Path) -> None:
        bundle.write_json(output, transcript)

    bundle.run_stage(
        f"transcripts/ext-{context_id}",
        inputs={f"context/{context_id}": sha256_file(bundle.abspath(source_rel))},
        params={"parser": fmt, "version": int(PARSER_VERSION)},
        producer=(transcript.engine.name, transcript.engine.version),
        output=output,
        fn=produce,
    )


def read_context_file(file_path: str) -> dict[str, Any]:
    """Read a local file for ``file_path``: absolute, regular, not hidden, at most 16 MiB.

    ``~`` is not expanded (the contract asks for an absolute path). Hidden path components
    (``~/.ssh``, ``~/.aws``, …) are refused on the given *and* the resolved path so a symlink
    cannot smuggle credentials into a bundle that later feeds an LLM.
    """
    path = Path(file_path)
    if not path.is_absolute():
        raise InvalidArgumentError(
            "file_path must be an absolute path (~ is not expanded)",
            details={"file_path": file_path},
        )
    if not path.exists():
        raise NotFoundError(f"file not found: {file_path}", details={"file_path": file_path})
    resolved = path.resolve()
    hidden = [part for part in (*path.parts[1:], *resolved.parts[1:]) if part.startswith(".")]
    if hidden:
        raise InvalidArgumentError(
            "file_path must not contain hidden path components",
            details={"file_path": file_path, "hidden": sorted(set(hidden))},
        )
    if not resolved.is_file():
        raise InvalidArgumentError(
            "file_path must point to a regular file", details={"file_path": file_path}
        )
    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise InvalidArgumentError(
            f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB ({size} bytes)",
            details={"file_path": file_path, "bytes": size, "max_bytes": MAX_FILE_BYTES},
        )
    data = resolved.read_bytes()
    payload: dict[str, Any] = {"file_path": str(path), "bytes": len(data)}
    try:
        payload["content"] = data.decode("utf-8")
    except UnicodeDecodeError:
        payload["content"] = base64.b64encode(data).decode("ascii")
        payload["content_encoding"] = "base64"
    return payload
