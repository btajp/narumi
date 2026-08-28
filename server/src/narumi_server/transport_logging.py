"""Do not persist MCP payloads or HTTP headers through dependency diagnostics."""

from __future__ import annotations

import logging

_SENSITIVE_NAMESPACES = frozenset(
    {"mcp", "mcp_types", "httpx", "httpx2", "httpcore", "httpcore2", "uvicorn"}
)


class TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.split(".", 1)[0] in _SENSITIVE_NAMESPACES:
            record.msg = "Transport diagnostic (payload and exception details withheld)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_transport_log_filters() -> None:
    """Filter handlers, not parent loggers: propagated child records bypass logger filters."""
    handlers = list(logging.getLogger().handlers)
    for value in logging.Logger.manager.loggerDict.values():
        if (
            isinstance(value, logging.Logger)
            and value.name.split(".", 1)[0] in _SENSITIVE_NAMESPACES
        ):
            handlers.extend(value.handlers)
    for handler in handlers:
        if not any(isinstance(item, TransportLogFilter) for item in handler.filters):
            handler.addFilter(TransportLogFilter())
