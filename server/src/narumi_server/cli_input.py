"""Contract-derived CLI options, explicit nullable values and secret-safe input parsing."""

from __future__ import annotations

import getpass
import json
import sys
import uuid
import warnings
from dataclasses import dataclass
from typing import Any

import click
from jsonschema import Draft202012Validator
from narumi.contracts import ToolContract
from narumi.errors import InvalidArgumentError

HELP_TEXT_LIMIT = 80
KIND_STRING = "string"
KIND_INTEGER = "integer"
KIND_NUMBER = "number"
KIND_BOOLEAN = "boolean"
KIND_JSON = "json"
KIND_FLEXIBLE = "flexible"
_LOCAL_DEF_PREFIX = "#/$defs/"
_JSON_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
MAX_STDIN_CHARACTERS = 1_048_576


class NullOption(click.Option):
    """An auxiliary flag that sends null for one contract property, never a new property."""

    def __init__(self, prop: str, flag: str, name: str) -> None:
        self.property_name = prop
        super().__init__(
            [flag, name],
            is_flag=True,
            default=False,
            help=f"Send null for {_flag(prop)}; cannot combine with a value for that option.",
        )


class SecretStdinOption(click.Option):
    """Read a write-only property from stdin without accepting an argv value."""

    def __init__(self, prop: str, flag: str, name: str) -> None:
        self.property_name = prop
        super().__init__(
            [flag, name],
            is_flag=True,
            default=False,
            help=f"Read {_flag(prop)} from stdin; do not include the secret in the command line.",
        )


@dataclass(frozen=True)
class ToolInput:
    options: list[click.Option]
    kinds: dict[str, str]
    null_options: dict[str, NullOption]
    required_nullable: frozenset[str]
    secret_options: dict[str, SecretStdinOption]
    required_secret: frozenset[str]


def _flag(prop: str) -> str:
    return "--" + prop.replace("_", "-")


def _resolve_schema(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow the loader's local refs for CLI type and help presentation."""
    seen: set[str] = set()
    current = schema
    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        ref = current["$ref"]
        name = ref.removeprefix(_LOCAL_DEF_PREFIX)
        if not ref.startswith(_LOCAL_DEF_PREFIX) or name in seen:
            break
        seen.add(name)
        target = defs.get(name)
        if not isinstance(target, dict):
            break
        current = {**target, **{k: v for k, v in current.items() if k != "$ref"}}
    return current


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return KIND_BOOLEAN
    if isinstance(value, str):
        return KIND_STRING
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return KIND_INTEGER if isinstance(value, int) or value.is_integer() else KIND_NUMBER


def _schema_types(
    schema: dict[str, Any] | bool, defs: dict[str, Any], seen: frozenset[str] = frozenset()
) -> set[str]:
    if isinstance(schema, bool):
        return set(_JSON_TYPES) if schema else set()
    declared = schema.get("type")
    types = {declared} if isinstance(declared, str) else set(declared or _JSON_TYPES)
    if KIND_NUMBER in types:
        types.add(KIND_INTEGER)
    ref = schema.get("$ref", "")
    name = ref.removeprefix(_LOCAL_DEF_PREFIX)
    if ref.startswith(_LOCAL_DEF_PREFIX) and name not in seen and name in defs:
        types &= _schema_types(defs[name], defs, seen | {name})
    if "const" in schema:
        types &= {_value_type(schema["const"])}
    if "enum" in schema:
        types &= {_value_type(value) for value in schema["enum"]}
    for variant in schema.get("allOf", []):
        types &= _schema_types(variant, defs, seen)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            types &= set().union(*(_schema_types(item, defs, seen) for item in schema[keyword]))
    return types


def option_kind(schema: dict[str, Any] | bool, defs: dict[str, Any]) -> str:
    """Keep scalar types; objects/arrays take JSON, string alternatives also accept text."""
    types = _schema_types(schema, defs) - {"null"}
    if types and types <= {KIND_INTEGER, KIND_NUMBER}:
        return KIND_NUMBER if KIND_NUMBER in types else KIND_INTEGER
    if len(types) == 1:
        single = next(iter(types))
        if single in (KIND_STRING, KIND_INTEGER, KIND_NUMBER, KIND_BOOLEAN):
            return single
        return KIND_JSON
    return KIND_FLEXIBLE if KIND_STRING in types else KIND_JSON


def accepts_null(schema: dict[str, Any] | bool, defs: dict[str, Any]) -> bool:
    """Use JSON Schema semantics, including refs, unions and restricting sibling keywords."""
    return Draft202012Validator({"$defs": defs, "allOf": [schema]}).is_valid(None)


def _help_text(schema: dict[str, Any] | bool, defs: dict[str, Any], prop: str, kind: str) -> str:
    resolved = _resolve_schema(schema, defs) if isinstance(schema, dict) else {}
    description = resolved.get("description")
    text = " ".join(str(description).split()) if isinstance(description, str) else prop
    if len(text) > HELP_TEXT_LIMIT:
        text = text[: HELP_TEXT_LIMIT - 1].rstrip() + "…"
    if prop == "request_id":
        return f"{text} [default: generated UUID4]"
    suffix = {KIND_JSON: " [JSON]", KIND_FLEXIBLE: " [value or JSON]"}.get(kind, "")
    return text + suffix


def _available_clear_flag(prop: str, occupied: set[str]) -> str:
    return _available_flag("--clear-" + prop.replace("_", "-"), occupied)


def _available_flag(preferred: str, occupied: set[str]) -> str:
    if preferred not in occupied:
        return preferred
    fallback = preferred + "-value"
    candidate, index = fallback, 2
    while candidate in occupied:
        candidate = f"{fallback}-{index}"
        index += 1
    return candidate


def _has_write_only(schema: Any, defs: dict[str, Any], seen: frozenset[str] = frozenset()) -> bool:
    if isinstance(schema, list):
        return any(_has_write_only(item, defs, seen) for item in schema)
    if not isinstance(schema, dict):
        return False
    if schema.get("writeOnly") is True:
        return True
    ref = schema.get("$ref", "")
    if isinstance(ref, str) and ref.startswith(_LOCAL_DEF_PREFIX):
        name = ref.removeprefix(_LOCAL_DEF_PREFIX)
        if name not in seen and _has_write_only(defs.get(name), defs, seen | {name}):
            return True
    return any(
        _has_write_only(value, defs, seen)
        for key, value in schema.items()
        if key not in {"$ref", "$defs", "const", "enum", "default", "examples"}
    )


def contains_secret_value(contract: ToolContract, arguments: dict[str, Any]) -> bool:
    """A literal JSON argument must not supply a write-only property or its container."""
    schema = contract.input_schema
    defs = schema.get("$defs", {})
    return any(
        arguments.get(prop) is not None and _has_write_only(prop_schema, defs)
        for prop, prop_schema in schema.get("properties", {}).items()
    )


def secret_strings(contract: ToolContract, arguments: dict[str, Any]) -> tuple[str, ...]:
    """Keep submitted secret strings only in memory to reject accidental response echoes."""

    def strings(value: Any) -> list[str]:
        found, pending = [], [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and item:
                found.append(item)
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return found

    schema = contract.input_schema
    return tuple(
        value
        for prop, prop_schema in schema.get("properties", {}).items()
        if _has_write_only(prop_schema, schema.get("$defs", {}))
        for value in strings(arguments.get(prop))
    )


def build_tool_input(contract: ToolContract) -> ToolInput:
    """Generate value, secret-prompt/stdin and null controls from the same contract."""
    schema = contract.input_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    defs: dict[str, Any] = schema.get("$defs", {})
    nullable = {prop for prop, value in properties.items() if accepts_null(value, defs)}
    secret = {prop for prop, value in properties.items() if _has_write_only(value, defs)}
    options: list[click.Option] = []
    kinds: dict[str, str] = {}
    for prop, prop_schema in properties.items():
        kind = kinds[prop] = option_kind(prop_schema, defs)
        settings: dict[str, Any] = {
            "default": None,
            "required": prop in required and prop != "request_id" and prop not in nullable,
            "help": _help_text(prop_schema, defs, prop, kind),
        }
        flag = _flag(prop)
        if prop in secret:
            settings.update(
                required=False,
                default=False,
                help=f"Prompt without echo for {prop}; omit to leave it unchanged.",
            )
            option = click.Option([flag], is_flag=True, **settings)
        elif kind == KIND_BOOLEAN:
            option = click.Option([f"{flag}/--no-{prop.replace('_', '-')}"], **settings)
        elif kind == KIND_INTEGER:
            option = click.Option([flag], type=click.INT, **settings)
        elif kind == KIND_NUMBER:
            option = click.Option([flag], type=click.FLOAT, **settings)
        else:
            option = click.Option([flag], type=click.STRING, **settings)
        option.name = prop  # preserve the schema's name instead of Click's underscore normalization
        options.append(option)

    # Reserve all real flags first; a contract's clear_url property must keep --clear-url.
    occupied_flags = {"--help", "-h"} | {
        flag for option in options for flag in (*option.opts, *option.secondary_opts)
    }
    occupied_names = set(properties) | {option.name for option in options}
    secret_options: dict[str, SecretStdinOption] = {}
    for index, prop in enumerate(properties):
        if prop not in secret:
            continue
        flag = _available_flag(_flag(prop) + "-stdin", occupied_flags)
        name = f"_narumi_stdin_{index}"
        while name in occupied_names:
            name = "_" + name
        option = SecretStdinOption(prop, flag, name)
        secret_options[prop] = option
        options.append(option)
        occupied_flags.add(flag)
        occupied_names.add(name)
    null_options: dict[str, NullOption] = {}
    for index, prop in enumerate(properties):
        if prop not in nullable:
            continue
        flag = _available_clear_flag(prop, occupied_flags)
        name = f"_narumi_clear_{index}"
        while name in occupied_names:
            name = "_" + name
        option = NullOption(prop, flag, name)
        if prop in required and prop != "request_id":
            options[index].help += f" [required: value or {flag}]"
        options.append(option)
        null_options[prop] = option
        occupied_flags.add(flag)
        occupied_names.add(name)
    return ToolInput(
        options=options,
        kinds=kinds,
        null_options=null_options,
        required_nullable=frozenset((required & nullable) - {"request_id"}),
        secret_options=secret_options,
        required_secret=frozenset(required & secret),
    )


def parse_json_option(prop: str, value: str, *, redact: bool = False) -> Any:
    try:
        return json.loads(value)
    except (ValueError, RecursionError) as exc:
        if redact:
            raise InvalidArgumentError(
                f"{_flag(prop)} must be a JSON document", details={"option": prop}
            ) from None
        if isinstance(exc, RecursionError):
            raise
        raise InvalidArgumentError(
            f"{_flag(prop)} must be a JSON document: {exc}",
            details={"option": prop, "value": value},
        ) from exc


def _parse_flexible_option(value: str) -> Any:
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    # A string-capable option never treats the literal word null as a clearing command.
    return value if parsed is None else parsed


def read_stdin() -> str:
    """Read a bounded document without copying it to argv, the environment or a file."""
    stream = sys.stdin
    if stream.isatty():
        raise InvalidArgumentError("Use a hidden prompt for interactive secret input")
    try:
        value = stream.read(MAX_STDIN_CHARACTERS + 1)
    except (OSError, UnicodeError):
        raise InvalidArgumentError("Could not read stdin") from None
    except (EOFError, KeyboardInterrupt):
        raise InvalidArgumentError("stdin input was cancelled") from None
    if len(value) > MAX_STDIN_CHARACTERS:
        raise InvalidArgumentError("stdin exceeds the input size limit")
    return value


def _read_secret(prop: str, *, from_stdin: bool) -> str:
    if from_stdin:
        value = read_stdin()
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = click.prompt(prop, hide_input=True, err=True, type=str)
        except getpass.GetPassWarning:
            raise InvalidArgumentError("Terminal echo could not be disabled; use stdin") from None
        except (click.Abort, EOFError, KeyboardInterrupt):
            raise InvalidArgumentError("Secret input was cancelled") from None
    if not value:
        raise InvalidArgumentError("Secret input must not be empty")
    return value


def collect_args(contract: ToolContract, spec: ToolInput, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Assemble only real properties; omission, literal text and explicit null stay distinct."""
    if sum(bool(kwargs.get(option.name)) for option in spec.secret_options.values()) > 1:
        raise InvalidArgumentError("Only one secret property can read stdin; use --json-stdin")
    args: dict[str, Any] = {}
    for prop, kind in spec.kinds.items():
        value = kwargs.get(prop)
        null_option = spec.null_options.get(prop)
        clear = null_option is not None and kwargs.get(null_option.name, False)
        secret_option = spec.secret_options.get(prop)
        if secret_option is not None:
            prompt, from_stdin = bool(value), bool(kwargs.get(secret_option.name))
            if sum((prompt, from_stdin, bool(clear))) > 1:
                raise InvalidArgumentError(
                    "Secret prompt, stdin and clearing are mutually exclusive"
                )
            value = _read_secret(prop, from_stdin=from_stdin) if prompt or from_stdin else None
            if value is None and not clear and prop in spec.required_secret:
                raise InvalidArgumentError(f"{_flag(prop)} or {secret_option.opts[0]} is required")
        if clear:
            if value is not None:
                raise InvalidArgumentError(
                    f"{_flag(prop)} cannot be combined with {null_option.opts[0]}",
                    details={"option": prop},
                )
            args[prop] = None
        elif value is not None:
            if kind == KIND_JSON:
                args[prop] = parse_json_option(prop, value, redact=contract.has_write_only_input)
            elif kind == KIND_FLEXIBLE:
                args[prop] = _parse_flexible_option(value)
            else:
                args[prop] = value
        elif prop in spec.required_nullable:
            raise InvalidArgumentError(
                f"{_flag(prop)} or {null_option.opts[0]} is required", details={"option": prop}
            )
    return with_request_id(contract, args)


def with_request_id(contract: ToolContract, args: dict[str, Any]) -> dict[str, Any]:
    if "request_id" in contract.input_schema.get("properties", {}) and "request_id" not in args:
        args["request_id"] = str(uuid.uuid4())
    return args
