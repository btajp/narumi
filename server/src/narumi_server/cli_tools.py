"""Product CLI ``narumi``: a contract-driven 1:1 mapping of the MCP tools.

Subcommands are generated from ``contracts/`` at start-up — one per tool
(``list_meetings`` → ``narumi list-meetings``) plus the generic escape hatch
``narumi tool <name> --json '{...}'``. The CLI never calls the library directly
(that is ``narumi-dev``): a call goes to a running ``narumi-server`` when the
``--server-url`` endpoint answers an MCP probe (``initialize`` +
``get_server_info`` with ~1s timeouts), otherwise it runs in this process
through the same ``narumi_server.app.dispatch`` code path the server uses.
``--require-server`` / ``--in-process`` force either side. Recording tools
(``start_recording`` / ``stop_recording`` / ``get_recording_status``) are
refused in-process: the recording would die with the CLI process.

Success prints the tool's structured content as JSON on stdout (``--pretty``
by default, ``--raw`` for one line); every failure prints the contract
``error_envelope`` on stderr and exits 2.
"""

from __future__ import annotations

import contextlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any

import click
from narumi.config import DEFAULT_HTTP_PORT, ENV_HOME
from narumi.contracts import ContractSet, ToolContract, load_contracts
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError

from narumi_server import __version__
from narumi_server.app import dispatch
from narumi_server.context import build_context

ENV_SERVER_URL = "NARUMI_SERVER_URL"
DEFAULT_SERVER_URL = f"http://127.0.0.1:{DEFAULT_HTTP_PORT}/mcp"
ERROR_EXIT_CODE = 2
TRANSPORT_CLI = "cli"
RECORDING_TOOLS = frozenset({"start_recording", "stop_recording", "get_recording_status"})
"""Tools that need the resident server: in-process the recorder dies with the CLI process."""

MODE_AUTO = "auto"
MODE_IN_PROCESS = "in-process"
MODE_REQUIRE_SERVER = "require-server"

PROTOCOL_VERSION = "2025-06-18"
PROBE_TIMEOUT = 1.0
"""Seconds for each probe round-trip (connect + ``initialize``)."""
PROBE_CALL_TIMEOUT = 2.0
"""Seconds for the probe's ``get_server_info`` call (it may run ``narumi-recorder check``)."""
CALL_TIMEOUT = 600.0
"""Bound for the real tool call. Generous: tools enqueue jobs instead of awaiting them."""

HELP_TEXT_LIMIT = 80

KIND_STRING = "string"
KIND_INTEGER = "integer"
KIND_NUMBER = "number"
KIND_BOOLEAN = "boolean"
KIND_JSON = "json"  # array / object: the option value must be a JSON document
KIND_FLEXIBLE = "flexible"  # oneOf string | array/object: JSON if it parses, else the raw string

GENERIC_COMMAND = "tool"

_LOCAL_DEF_PREFIX = "#/$defs/"
_JSON_CONTENT_TYPE = "application/json"
_SSE_CONTENT_TYPE = "text/event-stream"


@dataclass(frozen=True)
class CliState:
    """Global options resolved by the group callback, shared by every subcommand."""

    server_url: str
    mode: str
    data_root: Path | None
    pretty: bool


# ---------------------------------------------------------------------------- schema → options
def _resolve_schema(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow local ``$ref``s (the loader inlined everything into ``$defs``).

    Keys written next to the ``$ref`` (typically ``description``) win over the target's.
    """
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


def _schema_types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    return set()


def option_kind(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """Map one ``inputSchema`` property to a CLI option kind.

    string / integer / number / boolean stay typed; array / object take a JSON string;
    a ``oneOf`` / ``anyOf`` that also admits a plain string (e.g. the ``scope`` selector)
    accepts either the raw string or a JSON document.
    """
    resolved = _resolve_schema(schema, defs)
    types = _schema_types(resolved) - {"null"}
    if len(types) == 1:
        single = next(iter(types))
        if single in (KIND_STRING, KIND_INTEGER, KIND_NUMBER, KIND_BOOLEAN):
            return single
        return KIND_JSON
    if types:  # e.g. ["string", "integer"] — no such contract today; JSON keeps it explicit
        return KIND_FLEXIBLE if KIND_STRING in types else KIND_JSON
    variant_types: set[str] = set()
    for variant in resolved.get("oneOf") or resolved.get("anyOf") or []:
        if isinstance(variant, dict):
            variant_types |= _schema_types(_resolve_schema(variant, defs))
    variant_types -= {"null"}
    if variant_types == {KIND_STRING}:
        return KIND_STRING
    if KIND_STRING in variant_types:
        return KIND_FLEXIBLE
    return KIND_JSON


def _help_text(schema: dict[str, Any], defs: dict[str, Any], prop: str, kind: str) -> str:
    resolved = _resolve_schema(schema, defs)
    description = resolved.get("description")
    text = " ".join(str(description).split()) if isinstance(description, str) else prop
    if len(text) > HELP_TEXT_LIMIT:
        text = text[: HELP_TEXT_LIMIT - 1].rstrip() + "…"
    if prop == "request_id":
        return f"{text} [default: generated UUID4]"
    suffix = {KIND_JSON: " [JSON]", KIND_FLEXIBLE: " [value or JSON]"}.get(kind, "")
    return text + suffix


def _tool_options(contract: ToolContract) -> tuple[list[click.Option], dict[str, str]]:
    """Click options for every ``inputSchema`` property, plus each property's kind."""
    schema = contract.input_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    defs: dict[str, Any] = schema.get("$defs", {})
    options: list[click.Option] = []
    kinds: dict[str, str] = {}
    for prop, prop_schema in properties.items():
        kind = option_kind(prop_schema, defs)
        kinds[prop] = kind
        flag = "--" + prop.replace("_", "-")
        settings: dict[str, Any] = {
            "default": None,
            "required": prop in required and prop != "request_id",
            "help": _help_text(prop_schema, defs, prop, kind),
        }
        if kind == KIND_BOOLEAN:
            options.append(click.Option([f"{flag}/--no-{prop.replace('_', '-')}"], **settings))
        elif kind == KIND_INTEGER:
            options.append(click.Option([flag], type=click.INT, **settings))
        elif kind == KIND_NUMBER:
            options.append(click.Option([flag], type=click.FLOAT, **settings))
        else:
            options.append(click.Option([flag], type=click.STRING, **settings))
    return options, kinds


def _parse_json_option(prop: str, value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError as exc:
        raise InvalidArgumentError(
            f"--{prop.replace('_', '-')} must be a JSON document: {exc}",
            details={"option": prop, "value": value},
        ) from exc


def _parse_flexible_option(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value  # a plain string (e.g. `--scope cloudnative`)


def _collect_args(
    contract: ToolContract, kinds: dict[str, str], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Turn click's parsed options into tool arguments; omitted options stay omitted."""
    args: dict[str, Any] = {}
    for prop, value in kwargs.items():
        if value is None:
            continue
        kind = kinds.get(prop, KIND_STRING)
        if kind == KIND_JSON:
            args[prop] = _parse_json_option(prop, value)
        elif kind == KIND_FLEXIBLE:
            args[prop] = _parse_flexible_option(value)
        else:
            args[prop] = value
    return _with_request_id(contract, args)


def _with_request_id(contract: ToolContract, args: dict[str, Any]) -> dict[str, Any]:
    if "request_id" in contract.input_schema.get("properties", {}) and "request_id" not in args:
        args["request_id"] = str(uuid.uuid4())
    return args


# ---------------------------------------------------------------------------- HTTP client
class ServerUnreachableError(Exception):
    """The server URL did not answer as an MCP endpoint (connection, timeout or protocol)."""


def _sse_messages(text: str) -> list[Any]:
    """JSON payloads of the ``data:`` events in one SSE body (non-JSON events are skipped)."""
    messages: list[Any] = []
    data_lines: list[str] = []
    for raw_line in [*text.splitlines(), ""]:
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                with contextlib.suppress(ValueError):
                    messages.append(json.loads("\n".join(data_lines)))
                data_lines = []
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].removeprefix(" "))
    return messages


def _snippet(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    return text[:limit]


class McpHttpClient:
    """Minimal synchronous MCP Streamable HTTP client for one-shot CLI calls.

    Speaks just enough JSON-RPC: ``initialize`` → ``notifications/initialized`` →
    ``tools/call``, plus a best-effort DELETE to end the session. Accepts both
    ``application/json`` and ``text/event-stream`` responses and reuses the
    ``Mcp-Session-Id`` the server assigns.
    """

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise InvalidArgumentError(
                f"--server-url must be an http(s) URL: {url!r}", details={"server_url": url}
            )
        self.url = url
        self.session_id: str | None = None
        self.negotiated_version: str | None = None
        self._next_id = 0

    # -------------------------------------------------------------- wire helpers
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": _JSON_CONTENT_TYPE,
            "Accept": f"{_JSON_CONTENT_TYPE}, {_SSE_CONTENT_TYPE}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.negotiated_version:
            headers["MCP-Protocol-Version"] = self.negotiated_version
        return headers

    def _post(self, message: dict[str, Any], timeout: float) -> tuple[int, Message, bytes]:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(  # scheme checked in __init__
            self.url, data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            snippet = _snippet(exc.read())
            raise ServerUnreachableError(f"HTTP {exc.code} from {self.url}: {snippet}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            raise ServerUnreachableError(str(reason if reason is not None else exc)) from exc

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        status, headers, body = self._post(message, timeout)
        session = headers.get("mcp-session-id")
        if session:
            self.session_id = session
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        try:
            if content_type == _JSON_CONTENT_TYPE:
                candidates: list[Any] = [json.loads(body)]
            elif content_type == _SSE_CONTENT_TYPE:
                candidates = _sse_messages(body.decode("utf-8"))
            else:
                raise ServerUnreachableError(
                    f"unexpected response from {self.url} "
                    f"(status {status}, content type {content_type or 'missing'})"
                )
        except ValueError as exc:
            raise ServerUnreachableError(f"invalid JSON from {self.url}: {exc}") from exc
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("id") in (request_id, str(request_id))
                and ("result" in candidate or "error" in candidate)
            ):
                return candidate
        raise ServerUnreachableError(f"no JSON-RPC response to {method!r} from {self.url}")

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method}, PROBE_TIMEOUT)

    # -------------------------------------------------------------- MCP surface
    def initialize(self, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "narumi", "version": __version__},
            },
            timeout,
        )
        error = response.get("error")
        if error is not None:
            raise ServerUnreachableError(f"initialize failed: {error}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ServerUnreachableError(f"initialize returned no result from {self.url}")
        version = result.get("protocolVersion")
        if isinstance(version, str):
            self.negotiated_version = version
        self._notify("notifications/initialized")
        return result

    def probe(self) -> None:
        """``initialize`` + ``get_server_info`` with short timeouts.

        Raises :class:`ServerUnreachableError` when nothing MCP-shaped answers. A reachable
        server whose ``get_server_info`` returns an error envelope still counts as reachable.
        """
        self.initialize(PROBE_TIMEOUT)
        self._call_tool("get_server_info", {}, PROBE_CALL_TIMEOUT)

    def call_tool(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return self._call_tool(tool, args, CALL_TIMEOUT)

    def _call_tool(
        self, tool: str, args: dict[str, Any], timeout: float
    ) -> tuple[dict[str, Any], bool]:
        response = self._request("tools/call", {"name": tool, "arguments": args}, timeout)
        error = response.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            raise NarumiError(
                f"server rejected the call: {message or error}",
                code=ErrorCode.INTERNAL,
                details={"tool": tool, "jsonrpc_error": error},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ServerUnreachableError(f"tools/call returned no result from {self.url}")
        payload = result.get("structuredContent")
        if payload is None:
            payload = _payload_from_content(result)
        if not isinstance(payload, dict):
            raise NarumiError(
                f"tool {tool} returned no structured content",
                code=ErrorCode.INTERNAL,
                details={"tool": tool},
            )
        return payload, bool(result.get("isError", False))

    def close(self) -> None:
        """Best-effort ``DELETE`` ending the session; never raises."""
        if not self.session_id:
            return
        request = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
        with contextlib.suppress(Exception):
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
                response.read()


def _payload_from_content(result: dict[str, Any]) -> Any:
    """Fallback for servers that answer with text content only: parse the first text block."""
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------- execution
def _call(state: CliState, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Run one tool call: server first (unless ``--in-process``), else in this process."""
    if state.mode != MODE_IN_PROCESS:
        over_http = _call_over_http(state, tool, args)
        if over_http is not None:
            return over_http
    return _call_in_process(state, tool, args)


def _call_over_http(
    state: CliState, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], bool] | None:
    """``None`` when no server answers the probe and falling back in-process is allowed."""
    client = McpHttpClient(state.server_url)
    try:
        try:
            client.probe()
        except ServerUnreachableError as exc:
            if state.mode == MODE_REQUIRE_SERVER:
                raise NarumiError(
                    f"narumi-server at {state.server_url} is not reachable "
                    f"(--require-server): {exc}",
                    code=ErrorCode.INTERNAL,
                    details={"server_url": state.server_url},
                ) from exc
            return None
        try:
            return client.call_tool(tool, args)
        except ServerUnreachableError as exc:
            # The probe succeeded, so the call may have reached the server: surface the
            # failure instead of retrying in-process (the tool could run twice).
            raise NarumiError(
                f"lost the connection to narumi-server at {state.server_url}: {exc}",
                code=ErrorCode.INTERNAL,
                details={"server_url": state.server_url, "tool": tool},
            ) from exc
    finally:
        client.close()


def _call_in_process(
    state: CliState, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if tool in RECORDING_TOOLS:
        raise InvalidArgumentError(
            f"{tool} needs a resident narumi-server (a recording would stop with this CLI "
            "process). Start `narumi-server --http` or narumi.app and retry, or point "
            "--server-url / NARUMI_SERVER_URL at a running server.",
            details={"tool": tool, "server_url": state.server_url},
        )
    ctx = build_context(state.data_root, transports=[TRANSPORT_CLI])
    try:
        outcome = dispatch(ctx, tool, args)
        return outcome.payload, outcome.is_error
    finally:
        ctx.close()


def _render(payload: dict[str, Any], *, pretty: bool) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _run(state: CliState, tool: str, args: dict[str, Any]) -> None:
    """Execute and print: result JSON on stdout, error envelope on stderr with exit 2."""
    try:
        payload, is_error = _call(state, tool, args)
    except NarumiError as exc:
        payload, is_error = exc.to_payload(), True
    if is_error:
        click.echo(_render(payload, pretty=state.pretty), err=True)
        sys.exit(ERROR_EXIT_CODE)
    click.echo(_render(payload, pretty=state.pretty))


# ---------------------------------------------------------------------------- CLI assembly
def _make_tool_command(contract: ToolContract) -> click.Command:
    options, kinds = _tool_options(contract)

    def callback(**kwargs: Any) -> None:
        state: CliState = click.get_current_context().obj
        try:
            args = _collect_args(contract, kinds, kwargs)
        except NarumiError as exc:
            click.echo(_render(exc.to_payload(), pretty=state.pretty), err=True)
            sys.exit(ERROR_EXIT_CODE)
        _run(state, contract.name, args)

    return click.Command(
        name=contract.name.replace("_", "-"),
        params=list(options),
        callback=callback,
        help=contract.description,
        short_help=contract.title,
    )


def _make_generic_command(contracts: ContractSet) -> click.Command:
    def callback(name: str, args_json: str) -> None:
        state: CliState = click.get_current_context().obj
        tool = name.replace("-", "_")
        try:
            contract = contracts.get(tool)
            if contract is None:
                raise InvalidArgumentError(
                    f"unknown tool: {name}",
                    details={"tool": name, "known_tools": contracts.tool_names()},
                )
            args = _parse_json_option("json", args_json)
            if not isinstance(args, dict):
                raise InvalidArgumentError(
                    "--json must be a JSON object of tool arguments",
                    details={"tool": tool, "value": args_json},
                )
        except NarumiError as exc:
            click.echo(_render(exc.to_payload(), pretty=state.pretty), err=True)
            sys.exit(ERROR_EXIT_CODE)
        _run(state, tool, _with_request_id(contract, args))

    return click.Command(
        name=GENERIC_COMMAND,
        params=[
            click.Argument(["name"]),
            click.Option(
                ["--json", "args_json"],
                default="{}",
                show_default=True,
                help="Tool arguments as one JSON object (request_id is added when omitted).",
            ),
        ],
        callback=callback,
        help=(
            "Call any contract tool by name with raw JSON arguments, e.g. "
            "`narumi tool list_meetings --json '{\"limit\": 5}'`."
        ),
        short_help="Generic tool call (escape hatch)",
    )


def build_cli(contracts: ContractSet | None = None) -> click.Group:
    """Build the ``narumi`` group: one subcommand per contract tool plus ``tool``."""
    contract_set = contracts if contracts is not None else load_contracts()

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.version_option(__version__, prog_name="narumi")
    @click.option(
        "--server-url",
        envvar=ENV_SERVER_URL,
        show_envvar=True,
        default=DEFAULT_SERVER_URL,
        show_default=True,
        help="narumi-server Streamable HTTP endpoint tried first.",
    )
    @click.option(
        "--in-process",
        "mode",
        flag_value=MODE_IN_PROCESS,
        help="Never contact a server; run the tool in this process (recording tools refuse).",
    )
    @click.option(
        "--require-server",
        "mode",
        flag_value=MODE_REQUIRE_SERVER,
        help="Fail instead of falling back in-process when the server is unreachable.",
    )
    @click.option(
        "--data-root",
        type=click.Path(file_okay=False, path_type=Path),
        envvar=ENV_HOME,
        show_envvar=True,
        default=None,
        help="Data root for in-process execution (a server uses its own data root).",
    )
    @click.option(
        "--pretty/--raw",
        "pretty",
        default=True,
        help="Pretty-print the JSON output (--raw prints one line).",
    )
    @click.pass_context
    def cli(
        ctx: click.Context,
        server_url: str,
        mode: str | None,
        data_root: Path | None,
        pretty: bool,
    ) -> None:
        """narumi product CLI: the MCP tools from contracts/, one subcommand each.

        With a running narumi-server the call is sent over Streamable HTTP; otherwise it
        runs in-process against the same data root. Output is the tool's JSON result;
        errors are the contract error envelope on stderr with exit code 2.
        """
        ctx.obj = CliState(
            server_url=server_url,
            mode=mode or MODE_AUTO,
            data_root=data_root,
            pretty=pretty,
        )

    for contract in contract_set:
        cli.add_command(_make_tool_command(contract))
    cli.add_command(_make_generic_command(contract_set))
    return cli


def main() -> None:
    """Console-script entry point (``narumi``)."""
    try:
        cli = build_cli()
    except NarumiError as exc:  # contracts unreadable / inconsistent
        click.echo(json.dumps(exc.to_payload(), ensure_ascii=False), err=True)
        sys.exit(ERROR_EXIT_CODE)
    cli(prog_name="narumi")


if __name__ == "__main__":  # pragma: no cover
    main()
