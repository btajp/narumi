"""Bootstrap-authenticated local TLS connections shared by app, CLI and stdio bridge."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import secrets
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from narumi.errors import BusyError, NarumiError

from narumi_server.bootstrap import (
    BOOTSTRAP_FILE,
    BOOTSTRAP_VERSION,
    atomic_private_write,
    bootstrap_path,
    open_private_file,
    private_server_directory,
    read_bootstrap,
    read_client_bootstrap,
    write_bootstrap,
)
from narumi_server.transport_errors import (
    BootstrapNotFoundError,
    TransportSecurityError,
)
from narumi_server.transport_tls import (
    client_ssl_context,
    create_certificate,
    endpoint_for,
    secure_endpoint,
)

if TYPE_CHECKING:
    from narumi.providers.secrets import SecretStore

logger = logging.getLogger(__name__)

__all__ = [
    "BootstrapNotFoundError",
    "ClientTransport",
    "ServerLease",
    "ServerTransport",
    "TransportSecurityError",
    "acquire_server_lease",
    "load_client_transport",
    "prepare_server_transport",
]


def _default_secret_store() -> SecretStore:
    from narumi.providers.secrets import KeychainSecretStore

    return KeychainSecretStore()


def _token_account(root: Path, instance_id: str) -> str:
    namespace = hashlib.sha256(str(root.expanduser().resolve()).encode("utf-8")).hexdigest()
    return f"transport:{namespace}:{instance_id}"


def _instance_id(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except ValueError:
        raise TransportSecurityError() from None


def _validate_document(root: Path, document: dict[str, Any]) -> dict[str, Any]:
    if set(document) != {
        "version",
        "server_instance_id",
        "pid",
        "url",
        "certificate_sha256",
        "certificate_pem",
        "token_account",
    }:
        raise TransportSecurityError()
    instance = _instance_id(document["server_instance_id"])
    if (
        type(document["version"]) is not int
        or document["version"] != BOOTSTRAP_VERSION
        or type(document["pid"]) is not int
        or document["pid"] <= 0
        or document["token_account"] != _token_account(root, instance)
        or not isinstance(document["certificate_pem"], str)
        or not isinstance(document["certificate_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", document["certificate_sha256"])
    ):
        raise TransportSecurityError()
    secure_endpoint(document["url"])
    return document


@dataclass(frozen=True)
class ClientTransport:
    url: str
    server_instance_id: str
    certificate_sha256: str
    certificate_pem: str = field(repr=False)
    client_token: str = field(repr=False)
    ssl_context: ssl.SSLContext = field(repr=False, compare=False)


def load_client_transport(
    root: Path,
    *,
    expected_url: str | None = None,
    secret_store: SecretStore | None = None,
) -> ClientTransport:
    """Validate bootstrap and certificate before obtaining a token from Keychain.

    Only absence raises BootstrapNotFoundError. Invalid permissions, stale pins, HTTP
    endpoints, missing credentials and Keychain failures must never trigger a fallback.
    """
    document = _validate_document(root, read_client_bootstrap(root))
    url = document["url"]
    if expected_url is not None and secure_endpoint(expected_url) != url:
        raise TransportSecurityError()
    tls = client_ssl_context(document["certificate_pem"], document["certificate_sha256"], url)
    try:
        store = secret_store if secret_store is not None else _default_secret_store()
        token = store.get(document["token_account"])
    except Exception:
        raise TransportSecurityError() from None
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", token):
        raise TransportSecurityError()
    return ClientTransport(
        url=url,
        server_instance_id=document["server_instance_id"],
        certificate_sha256=document["certificate_sha256"],
        certificate_pem=document["certificate_pem"],
        client_token=token,
        ssl_context=tls,
    )


@dataclass
class ServerLease:
    """Hold before building a context: startup recovery writes to the shared catalog."""

    root: Path
    lock_fd: int = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> ServerLease:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.lock_fd)


def acquire_server_lease(root: Path) -> ServerLease:
    """One resident, developer-stdio or in-process server per data root; no Keychain I/O."""
    root = root.expanduser()
    with private_server_directory(root, create=True) as directory:
        lock_fd = open_private_file(directory, "server.lock", create=True)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            raise BusyError("A server already owns this data root") from None
        except OSError:
            os.close(lock_fd)
            raise TransportSecurityError() from None
    return ServerLease(root, lock_fd)


@dataclass
class ServerTransport:
    url: str
    server_instance_id: str
    certificate_sha256: str
    certificate_pem: str = field(repr=False)
    client_token: str = field(repr=False)
    certificate_path: Path
    private_key_path: Path = field(repr=False)
    token_account: str = field(repr=False)
    bootstrap_path: Path
    _root: Path = field(repr=False)
    _secret_store: SecretStore = field(repr=False)
    _lease: ServerLease = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> ServerTransport:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Remove only this instance's bootstrap and credentials, then release its lease."""
        if self._closed:
            return
        self._closed = True
        try:
            with private_server_directory(self._root) as directory:
                with contextlib.suppress(BootstrapNotFoundError):
                    document = read_bootstrap(directory)
                    if document.get("server_instance_id") == self.server_instance_id:
                        os.unlink(BOOTSTRAP_FILE, dir_fd=directory)
                for path in (self.certificate_path, self.private_key_path):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
        except (NarumiError, OSError):
            logger.warning("Local transport files could not be removed safely")
        try:
            self._secret_store.delete(self.token_account)
        except Exception:
            logger.warning("Local transport credential cleanup could not be completed")
        finally:
            self._lease.close()


def prepare_server_transport(
    root: Path,
    instance_id: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    secret_store: SecretStore | None = None,
) -> ServerTransport:
    """Acquire a root-specific lifetime lease, then atomically publish a fresh TLS identity."""
    instance_id = _instance_id(instance_id)
    url = endpoint_for(host, port, path)
    root = root.expanduser()
    lease = acquire_server_lease(root)
    try:
        store = secret_store if secret_store is not None else _default_secret_store()
        return _prepare_identity(root, instance_id, url, store, lease)
    except BaseException:
        lease.close()
        raise


def _prepare_identity(
    root: Path, instance_id: str, url: str, store: SecretStore, lease: ServerLease
) -> ServerTransport:
    account = _token_account(root, instance_id)
    with private_server_directory(root, create=True) as directory:
        previous: dict[str, Any] | None = None
        credential_written = False
        created: list[str] = []
        try:
            with contextlib.suppress(BootstrapNotFoundError):
                previous = _validate_document(root, read_bootstrap(directory))
            pem, private_pem, fingerprint = create_certificate(instance_id)
            token = secrets.token_urlsafe(48)
            try:
                store.set(account, token)
            except Exception:
                raise TransportSecurityError() from None
            credential_written = True
            for name, content in (
                (f"{instance_id}.pem", pem.encode("ascii")),
                (f"{instance_id}.key", private_pem),
            ):
                atomic_private_write(directory, name, content)
                created.append(name)
            write_bootstrap(
                directory,
                {
                    "version": BOOTSTRAP_VERSION,
                    "server_instance_id": instance_id,
                    "pid": os.getpid(),
                    "url": url,
                    "certificate_sha256": fingerprint,
                    "certificate_pem": pem,
                    "token_account": account,
                },
            )
            if previous is not None and previous["token_account"] != account:
                try:
                    store.delete(previous["token_account"])
                except Exception:
                    logger.warning("Previous local transport credential cleanup was unsuccessful")
            location = bootstrap_path(root)
            return ServerTransport(
                url=url,
                server_instance_id=instance_id,
                certificate_sha256=fingerprint,
                certificate_pem=pem,
                client_token=token,
                certificate_path=location.parent / f"{instance_id}.pem",
                private_key_path=location.parent / f"{instance_id}.key",
                token_account=account,
                bootstrap_path=location,
                _root=root,
                _secret_store=store,
                _lease=lease,
            )
        except BaseException:
            for name in created:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=directory)
            if credential_written:
                with contextlib.suppress(Exception):
                    store.delete(account)
            raise
