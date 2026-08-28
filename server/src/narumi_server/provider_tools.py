"""Provider operations share a confidential transport and error boundary."""

PROVIDER_TOOLS = frozenset(
    {
        "list_providers",
        "list_provider_connections",
        "set_provider_connection",
        "delete_provider_connection",
        "prepare_provider_runtime",
        "authenticate_provider_connection",
        "get_provider_auth_status",
        "test_provider_connection",
        "list_provider_models",
    }
)
