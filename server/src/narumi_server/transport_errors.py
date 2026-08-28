"""Value-free failures for local authenticated transports."""

from narumi.errors import AuthenticationRequiredError, EngineUnavailableError


class TransportSecurityError(AuthenticationRequiredError):
    def __init__(self) -> None:
        super().__init__("The authenticated local server connection could not be verified")


class BootstrapNotFoundError(EngineUnavailableError):
    def __init__(self) -> None:
        super().__init__("No authenticated local server bootstrap is available")


class SecureTransportUnavailableError(EngineUnavailableError):
    def __init__(self) -> None:
        super().__init__("Authenticated local transport dependencies are unavailable")
