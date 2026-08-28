"""Contract loader: reads ``contracts/``, inlines shared ``$defs`` and validates tool I/O.

The contract files are the source of truth for the MCP surface (AGENTS.md). This module turns them
into self-contained JSON Schemas (every external ``$ref`` inlined) so the server can register tools
and validate calls without touching the filesystem again. Any inconsistency between
``manifest.json``, ``tools/*.json`` and ``defs/*.json`` is a :class:`ContractMismatchError` at load
time — never a silent fallback.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError, best_match

from narumi.config import contracts_dir as default_contracts_dir
from narumi.errors import ContractMismatchError, InvalidArgumentError

MANIFEST_FILE = "manifest.json"
TOOLS_DIR = "tools"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
ANNOTATION_KEYS: tuple[str, ...] = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)
REQUIRED_TOOL_KEYS: tuple[str, ...] = ("name", "title", "description", "annotations", "inputSchema")
EXAMPLE_KEYS: tuple[str, ...] = ("input", "output")

_LOCAL_DEF_PREFIX = "#/$defs/"
_EXTERNAL_REF_RE = re.compile(r"^(?P<file>[^#]+)#/\$defs/(?P<name>[A-Za-z0-9_]+)$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
# Keys whose values are *data*, not subschemas: a "$ref" string inside them must be left alone.
_DATA_KEYS = frozenset({"const", "enum", "default", "examples"})
_DEVICE_AUTH_TOOLS = frozenset({"authenticate_provider_connection", "get_provider_auth_status"})

JsonDict = dict[str, Any]


# ---------------------------------------------------------------------------- format checking
def _is_rfc3339_datetime(instance: object) -> bool:
    """``date-time`` check without the optional ``rfc3339-validator`` dependency."""
    if not isinstance(instance, str):
        return True
    if not _RFC3339_RE.match(instance):
        return False
    datetime.fromisoformat(instance.upper())  # rejects impossible dates (ValueError)
    return True


def build_format_checker() -> FormatChecker:
    """A :class:`FormatChecker` that always enforces ``format: date-time``.

    jsonschema only checks ``date-time`` when ``rfc3339-validator`` happens to be installed; the
    contracts rely on it for every timestamp, so we register our own checker on the instance.
    """
    checker = FormatChecker()
    checker.checks("date-time", raises=ValueError)(_is_rfc3339_datetime)
    return checker


# ---------------------------------------------------------------------------- data classes
@dataclass(frozen=True)
class ToolContract:
    """One tool from ``contracts/tools/<name>.json`` with self-contained schemas."""

    name: str
    title: str
    description: str
    annotations: JsonDict
    input_schema: JsonDict
    output_schema: JsonDict | None
    examples: dict[str, list[JsonDict]]
    path: Path

    @property
    def read_only(self) -> bool:
        return bool(self.annotations.get("readOnlyHint", False))

    @property
    def has_write_only_input(self) -> bool:
        """Whether inputs contain write-only fields for transport and CLI secret handling."""
        return _contains_write_only(self.input_schema)

    @property
    def redact_validation_errors(self) -> bool:
        """Never reflect API keys or transient device-login data from malformed input/output."""
        return self.has_write_only_input or self.name in _DEVICE_AUTH_TOOLS

    @property
    def input_examples(self) -> list[JsonDict]:
        return list(self.examples.get("input", []))

    @property
    def output_examples(self) -> list[JsonDict]:
        return list(self.examples.get("output", []))

    def tool_definition(self) -> JsonDict:
        """MCP ``Tool`` fields (camelCase) — feed to ``mcp_types.Tool.model_validate``."""
        tool: JsonDict = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": copy.deepcopy(self.input_schema),
            "annotations": dict(self.annotations),
        }
        if self.output_schema is not None:
            tool["outputSchema"] = copy.deepcopy(self.output_schema)
        return tool


class ContractSet:
    """All tools of one contract version plus the shared definitions."""

    def __init__(
        self,
        *,
        name: str,
        contract_version: str,
        tools: Mapping[str, ToolContract],
        defs: Mapping[str, Any],
        path: Path | None = None,
    ) -> None:
        self.name = name
        self.contract_version = contract_version
        self.tools: dict[str, ToolContract] = dict(tools)
        self.defs: JsonDict = copy.deepcopy(dict(defs))
        self.path = path
        self._inliner = _Inliner(merged_defs=self.defs)
        self._format_checker = build_format_checker()
        self._input_validators = {
            tool_name: self._validator(tool.input_schema) for tool_name, tool in self.tools.items()
        }
        self._output_validators = {
            tool_name: self._validator(tool.output_schema)
            for tool_name, tool in self.tools.items()
            if tool.output_schema is not None
        }
        self._error_schema = self.schema_for_def("error_envelope")
        self._error_validator = self._validator(self._error_schema)

    # ------------------------------------------------------------------ container protocol
    def tool_names(self) -> list[str]:
        return list(self.tools)

    def get(self, tool: str) -> ToolContract | None:
        return self.tools.get(tool)

    def __contains__(self, tool: object) -> bool:
        return tool in self.tools

    def __getitem__(self, tool: str) -> ToolContract:
        return self.tools[tool]

    def __iter__(self) -> Iterator[ToolContract]:
        return iter(self.tools.values())

    def __len__(self) -> int:
        return len(self.tools)

    # ------------------------------------------------------------------ schemas
    def schema_for_def(self, name: str) -> JsonDict:
        """Self-contained JSON Schema for a shared definition (e.g. ``meeting_summary``)."""
        return self._inliner.inline_def(name)

    def error_envelope_schema(self) -> JsonDict:
        """Self-contained schema of ``{"error": {"code", "message", "details?"}}``."""
        return copy.deepcopy(self._error_schema)

    # ------------------------------------------------------------------ validation
    def validate_input(self, tool: str, args: Mapping[str, Any] | None) -> None:
        """Raise :class:`InvalidArgumentError` on bad args.

        ``details`` is ``{"tool": <name>, "errors": [{"path", "message", "validator"}, ...]}``.
        """
        validator = self._input_validators.get(tool)
        if validator is None:
            raise InvalidArgumentError(
                f"unknown tool: {tool}",
                details={
                    "tool": tool,
                    "errors": [{"path": "$", "message": f"unknown tool: {tool}", "validator": ""}],
                },
            )
        redact = self.tools[tool].redact_validation_errors
        summary, errors = _collect_errors(
            validator,
            {} if args is None else args,
            redact=redact,
        )
        if errors:
            error = InvalidArgumentError(
                f"invalid arguments for {tool}: {summary}",
                details={"tool": tool, "errors": errors},
            )
            if redact:
                raise error from None
            raise error

    def validate_output(self, tool: str, result: Mapping[str, Any]) -> None:
        """Raise :class:`ContractMismatchError` when a handler result violates ``outputSchema``."""
        if tool not in self.tools:
            raise ContractMismatchError(f"unknown tool: {tool}", details={"tool": tool})
        validator = self._output_validators.get(tool)
        if validator is None:
            return
        redact = self.tools[tool].redact_validation_errors
        summary, errors = _collect_errors(validator, result, redact=redact)
        if errors:
            error = ContractMismatchError(
                f"output of {tool} violates contract: {summary}",
                details={"tool": tool, "errors": errors},
            )
            if redact:
                raise error from None
            raise error

    def validate_error_envelope(self, payload: Mapping[str, Any]) -> None:
        """Raise :class:`ContractMismatchError` unless ``payload`` is a valid error envelope."""
        summary, errors = _collect_errors(self._error_validator, payload)
        if errors:
            raise ContractMismatchError(
                f"error envelope violates contract: {summary}", details={"errors": errors}
            )

    def check_examples(self) -> None:
        """Validate every ``examples.input`` / ``examples.output``; raise on the first failure."""
        for tool in self.tools.values():
            for index, example in enumerate(tool.input_examples):
                try:
                    self.validate_input(tool.name, example)
                except InvalidArgumentError as exc:
                    raise ContractMismatchError(
                        f"{tool.path.name}: examples.input[{index}] is invalid: {exc.message}",
                        details=exc.details,
                    ) from exc
            for index, example in enumerate(tool.output_examples):
                try:
                    self.validate_output(tool.name, example)
                except ContractMismatchError as exc:
                    raise ContractMismatchError(
                        f"{tool.path.name}: examples.output[{index}] is invalid: {exc.message}",
                        details=exc.details,
                    ) from exc

    # ------------------------------------------------------------------ internals
    def _validator(self, schema: JsonDict) -> Draft202012Validator:
        return Draft202012Validator(schema, format_checker=self._format_checker)


# ---------------------------------------------------------------------------- $ref inlining
@dataclass
class _InlineContext:
    """State for inlining one schema."""

    base_dir: Path | None
    local_defs: JsonDict
    pulled: JsonDict = field(default_factory=dict)
    local_refs: set[str] = field(default_factory=set)


class _Inliner:
    """Rewrites external ``$ref``s to ``#/$defs/<name>`` and copies the transitive closure."""

    def __init__(
        self,
        *,
        merged_defs: Mapping[str, Any],
        defs_by_file: Mapping[Path, Mapping[str, Any]] | None = None,
    ) -> None:
        self._merged = merged_defs
        self._defs_by_file = dict(defs_by_file or {})

    def inline(self, schema: Mapping[str, Any], *, base_dir: Path, where: str) -> JsonDict:
        """Return a deep copy of ``schema`` whose only ``$ref``s point into its own ``$defs``."""
        result = copy.deepcopy(dict(schema))
        local_defs = result.get("$defs", {})
        if not isinstance(local_defs, dict):
            raise ContractMismatchError(f"{where}: $defs must be an object")
        ctx = _InlineContext(base_dir=base_dir, local_defs=local_defs)
        self._walk(result, ctx, from_defs=False, where=where)
        missing = sorted(name for name in ctx.local_refs if name not in local_defs)
        if missing:
            raise ContractMismatchError(
                f"{where}: local $ref to undefined $defs: {missing}",
                details={"missing": missing},
            )
        all_defs = {**local_defs, **ctx.pulled}
        if all_defs:
            result["$defs"] = dict(sorted(all_defs.items()))
        return result

    def inline_def(self, name: str) -> JsonDict:
        """Self-contained schema for shared definition ``name``."""
        source = self._merged.get(name)
        if source is None:
            raise ContractMismatchError(f"unknown shared definition: {name}", details={"def": name})
        result = copy.deepcopy(source)
        ctx = _InlineContext(base_dir=None, local_defs={})
        self._walk(result, ctx, from_defs=True, where=f"$defs/{name}")
        if ctx.pulled:
            result["$defs"] = dict(sorted(ctx.pulled.items()))
        return result

    def _walk(self, node: Any, ctx: _InlineContext, *, from_defs: bool, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _DATA_KEYS:
                    continue
                if key == "$ref":
                    if not isinstance(value, str):
                        raise ContractMismatchError(f"{where}: $ref must be a string")
                    node[key] = self._resolve_ref(value, ctx, from_defs=from_defs, where=where)
                    continue
                self._walk(value, ctx, from_defs=from_defs, where=where)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, ctx, from_defs=from_defs, where=where)

    def _resolve_ref(self, ref: str, ctx: _InlineContext, *, from_defs: bool, where: str) -> str:
        if ref.startswith(_LOCAL_DEF_PREFIX):
            name = ref[len(_LOCAL_DEF_PREFIX) :]
            if not name or "/" in name:
                raise ContractMismatchError(f"{where}: unsupported $ref {ref!r}")
            if from_defs:
                self._pull(name, ctx, where=where)
            else:
                ctx.local_refs.add(name)
            return ref
        if ref.startswith("#"):
            raise ContractMismatchError(
                f"{where}: unsupported $ref {ref!r} (only #/$defs/<name> pointers are allowed)"
            )
        if from_defs or ctx.base_dir is None:
            raise ContractMismatchError(
                f"{where}: shared definitions must not reference other files: {ref!r}"
            )
        match = _EXTERNAL_REF_RE.match(ref)
        if match is None:
            raise ContractMismatchError(
                f"{where}: unsupported $ref {ref!r} (expected <file>#/$defs/<name>)"
            )
        target = (ctx.base_dir / match["file"]).resolve()
        file_defs = self._defs_by_file.get(target)
        if file_defs is None:
            raise ContractMismatchError(
                f"{where}: $ref {ref!r} points to a file not listed in manifest.defs",
                details={"ref": ref, "resolved": str(target)},
            )
        name = match["name"]
        if name not in file_defs:
            raise ContractMismatchError(
                f"{where}: unresolved $ref {ref!r}", details={"ref": ref, "def": name}
            )
        self._pull(name, ctx, where=where)
        return _LOCAL_DEF_PREFIX + name

    def _pull(self, name: str, ctx: _InlineContext, *, where: str) -> None:
        if name in ctx.pulled:
            return
        if name in ctx.local_defs:
            raise ContractMismatchError(
                f"{where}: $defs name {name!r} is defined both locally and in the shared defs"
            )
        source = self._merged.get(name)
        if source is None:
            raise ContractMismatchError(
                f"{where}: unresolved shared definition {name!r}", details={"def": name}
            )
        ctx.pulled[name] = None  # cycle guard; replaced below
        node = copy.deepcopy(source)
        self._walk(node, ctx, from_defs=True, where=f"$defs/{name}")
        ctx.pulled[name] = node


# ---------------------------------------------------------------------------- loading
def load_contracts(contracts_dir: Path | None = None) -> ContractSet:
    """Load ``manifest.json``, ``defs/*.json`` and ``tools/*.json`` into a :class:`ContractSet`.

    ``contracts_dir`` defaults to :func:`narumi.config.contracts_dir` (``NARUMI_CONTRACTS_DIR`` or
    the repository checkout).
    """
    root = Path(contracts_dir) if contracts_dir is not None else default_contracts_dir()
    if not root.is_dir():
        raise ContractMismatchError(
            f"contracts directory not found: {root}", details={"path": str(root)}
        )
    manifest = _read_json(root / MANIFEST_FILE)
    name = _require_str(manifest, "name", where=MANIFEST_FILE)
    contract_version = _require_str(manifest, "contract_version", where=MANIFEST_FILE)
    if not _SEMVER_RE.match(contract_version):
        raise ContractMismatchError(
            f"{MANIFEST_FILE}: contract_version {contract_version!r} is not semver"
        )
    tool_names = _require_str_list(manifest, "tools", where=MANIFEST_FILE)
    if len(set(tool_names)) != len(tool_names):
        raise ContractMismatchError(f"{MANIFEST_FILE}: duplicate tool names in 'tools'")
    defs_files = _require_str_list(manifest, "defs", where=MANIFEST_FILE)

    defs_by_file: dict[Path, JsonDict] = {}
    merged_defs: JsonDict = {}
    for rel in defs_files:
        path = (root / rel).resolve()
        document = _read_json(path)
        defs = document.get("$defs")
        if not isinstance(defs, dict):
            raise ContractMismatchError(f"{rel}: expected an object under '$defs'")
        for def_name, def_schema in defs.items():
            if def_name in merged_defs:
                raise ContractMismatchError(
                    f"{rel}: definition {def_name!r} is declared in more than one defs file"
                )
            if not isinstance(def_schema, dict | bool):
                raise ContractMismatchError(f"{rel}: $defs/{def_name} must be a schema")
            merged_defs[def_name] = def_schema
        defs_by_file[path] = defs

    tools_dir = root / TOOLS_DIR
    on_disk = sorted(p.stem for p in tools_dir.glob("*.json")) if tools_dir.is_dir() else []
    missing = [tool for tool in tool_names if tool not in on_disk]
    unlisted = [tool for tool in on_disk if tool not in tool_names]
    if missing or unlisted:
        raise ContractMismatchError(
            f"{MANIFEST_FILE} 'tools' and {TOOLS_DIR}/*.json disagree "
            f"(missing files: {missing}, unlisted files: {unlisted})",
            details={"missing_files": missing, "unlisted_files": unlisted},
        )

    inliner = _Inliner(merged_defs=merged_defs, defs_by_file=defs_by_file)
    tools = {tool: _load_tool(tools_dir / f"{tool}.json", inliner) for tool in tool_names}
    return ContractSet(
        name=name,
        contract_version=contract_version,
        tools=tools,
        defs=merged_defs,
        path=root,
    )


def _load_tool(path: Path, inliner: _Inliner) -> ToolContract:
    where = f"{TOOLS_DIR}/{path.name}"
    document = _read_json(path)
    for key in REQUIRED_TOOL_KEYS:
        if key not in document:
            raise ContractMismatchError(f"{where}: missing key {key!r}")
    name = document["name"]
    if not isinstance(name, str) or name != path.stem:
        raise ContractMismatchError(
            f"{where}: tool name {name!r} does not match the file name {path.stem!r}"
        )
    title = _require_str(document, "title", where=where)
    description = _require_str(document, "description", where=where)
    annotations = _load_annotations(document["annotations"], where=where)

    input_schema = _load_schema(document["inputSchema"], inliner, path, f"{where}#/inputSchema")
    raw_output = document.get("outputSchema")
    output_schema = (
        None
        if raw_output is None
        else _load_schema(raw_output, inliner, path, f"{where}#/outputSchema")
    )
    examples = _load_examples(document.get("examples"), where=where)
    return ToolContract(
        name=name,
        title=title,
        description=description,
        annotations=annotations,
        input_schema=input_schema,
        output_schema=output_schema,
        examples=examples,
        path=path,
    )


def _load_annotations(raw: Any, *, where: str) -> JsonDict:
    if not isinstance(raw, dict):
        raise ContractMismatchError(f"{where}: annotations must be an object")
    unknown = sorted(set(raw) - set(ANNOTATION_KEYS))
    if unknown:
        raise ContractMismatchError(f"{where}: unknown annotation keys {unknown}")
    for key in ANNOTATION_KEYS:
        if not isinstance(raw.get(key), bool):
            raise ContractMismatchError(f"{where}: annotations.{key} must be a boolean")
    return {key: raw[key] for key in ANNOTATION_KEYS}


def _load_schema(raw: Any, inliner: _Inliner, path: Path, where: str) -> JsonDict:
    if not isinstance(raw, dict):
        raise ContractMismatchError(f"{where}: schema must be an object")
    if raw.get("type") != "object":
        raise ContractMismatchError(f'{where}: MCP tool schemas must have "type": "object"')
    schema = inliner.inline(raw, base_dir=path.parent, where=where)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ContractMismatchError(
            f"{where}: invalid JSON Schema 2020-12: {exc.message}",
            details={"schema_path": [str(p) for p in exc.absolute_schema_path]},
        ) from exc
    return schema


def _load_examples(raw: Any, *, where: str) -> dict[str, list[JsonDict]]:
    if raw is None:
        return {"input": [], "output": []}
    if not isinstance(raw, dict):
        raise ContractMismatchError(f"{where}: examples must be an object")
    unknown = sorted(set(raw) - set(EXAMPLE_KEYS))
    if unknown:
        raise ContractMismatchError(f"{where}: unknown examples keys {unknown}")
    examples: dict[str, list[JsonDict]] = {}
    for key in EXAMPLE_KEYS:
        items = raw.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ContractMismatchError(f"{where}: examples.{key} must be a list of objects")
        examples[key] = [copy.deepcopy(item) for item in items]
    return examples


# ---------------------------------------------------------------------------- helpers
def _read_json(path: Path) -> JsonDict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractMismatchError(
            f"contract file not found: {path}", details={"path": str(path)}
        ) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractMismatchError(
            f"{path.name}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
            details={"path": str(path)},
        ) from exc
    if not isinstance(document, dict):
        raise ContractMismatchError(f"{path.name}: top-level value must be an object")
    return document


def _require_str(document: Mapping[str, Any], key: str, *, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractMismatchError(f"{where}: {key!r} must be a non-empty string")
    return value


def _require_str_list(document: Mapping[str, Any], key: str, *, where: str) -> list[str]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ContractMismatchError(f"{where}: {key!r} must be a list of non-empty strings")
    return list(value)


def _contains_write_only(node: Any) -> bool:
    if isinstance(node, dict):
        return node.get("writeOnly") is True or any(
            _contains_write_only(value) for key, value in node.items() if key not in _DATA_KEYS
        )
    if isinstance(node, list):
        return any(_contains_write_only(value) for value in node)
    return False


def _collect_errors(
    validator: Draft202012Validator, instance: Any, *, redact: bool = False
) -> tuple[str, list[JsonDict]]:
    """Return ``(summary, errors)``; ``errors`` is sorted by instance path for stable output."""
    found: list[ValidationError] = list(validator.iter_errors(instance))
    if not found:
        return "", []
    top = best_match(found)
    if redact:
        # A malformed secret or device code may be an object or may have appeared under an
        # unknown key. Never interpolate jsonschema's message, instance, or untrusted path.
        properties = validator.schema.get("properties", {})
        items = sorted(
            (_redacted_error_item(error, properties) for error in found),
            key=lambda item: (item["path"], item["message"]),
        )
        return _redacted_error_item(top, properties)["message"], items
    items = sorted(
        (_error_item(error) for error in found), key=lambda item: (item["path"], item["message"])
    )
    return top.message, items


def _redacted_error_item(error: ValidationError, properties: Mapping[str, Any]) -> JsonDict:
    first = next(iter(error.absolute_path), None)
    path = f"$.{first}" if isinstance(first, str) and first in properties else "$"
    validator = str(error.validator) if error.validator is not None else ""
    return {"path": path, "message": f"validation failed: {validator}", "validator": validator}


def _error_item(error: ValidationError) -> JsonDict:
    return {
        "path": error.json_path,
        "message": error.message,
        "validator": str(error.validator) if error.validator is not None else "",
    }
