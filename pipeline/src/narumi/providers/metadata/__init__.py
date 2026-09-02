"""Provider metadata and endpoint validation, independent of generation SDKs."""

from narumi.providers.metadata.client import MetadataClient
from narumi.providers.metadata.endpoints import (
    is_loopback_endpoint,
    validate_endpoint,
    validate_openai_compatible_endpoint,
)

__all__ = [
    "MetadataClient",
    "is_loopback_endpoint",
    "validate_endpoint",
    "validate_openai_compatible_endpoint",
]
