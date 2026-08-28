"""Ephemeral server certificates and exact local endpoint validation."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import ssl
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from narumi_server.transport_errors import (
    SecureTransportUnavailableError,
    TransportSecurityError,
)

NUMERIC_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def secure_endpoint(url: str) -> str:
    """Require one canonical numeric-loopback HTTPS endpoint, without DNS or redirects."""
    try:
        if (
            not isinstance(url, str)
            or not url
            or any(ord(char) <= 32 or ord(char) >= 127 for char in url)
            or any(char in url for char in "\\?#%")
        ):
            raise ValueError
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        if (
            parts.scheme != "https"
            or host not in NUMERIC_LOOPBACK_HOSTS
            or port is None
            or not 1 <= port <= 65535
            or parts.username is not None
            or parts.password is not None
            or not re.fullmatch(r"/[A-Za-z0-9_/-]*", parts.path)
            or "//" in parts.path
        ):
            raise ValueError
        authority = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
        if url != f"https://{authority}{parts.path}":
            raise ValueError
        return url
    except (ValueError, TypeError, AttributeError):
        raise TransportSecurityError() from None


def endpoint_for(host: str, port: int, path: str) -> str:
    if host not in NUMERIC_LOOPBACK_HOSTS or type(port) is not int:
        raise TransportSecurityError()
    authority = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
    return secure_endpoint(f"https://{authority}{path}")


def create_certificate(instance_id: str) -> tuple[str, bytes, str]:
    """Return public PEM, private PEM and the DER SHA256 pin (no system certificate store)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise SecureTransportUnavailableError() from None
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"narumi {instance_id}")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(host)) for host in NUMERIC_LOOPBACK_HOSTS]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    public_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return public_pem, private_pem, certificate.fingerprint(hashes.SHA256()).hex()


def client_ssl_context(pem: str, fingerprint: str, url: str) -> ssl.SSLContext:
    """Only this owner-verified self-signed certificate is a trust anchor.

    Do not load default CAs: an unrelated system-trusted certificate is not the local server.
    The caller may additionally compare the peer DER fingerprint after the TLS handshake.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import ExtendedKeyUsageOID
    except ImportError:
        raise SecureTransportUnavailableError() from None
    try:
        if (
            not isinstance(pem, str)
            or len(pem) > 8192
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or pem.count("-----BEGIN CERTIFICATE-----") != 1
        ):
            raise ValueError
        certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
        actual = certificate.fingerprint(hashes.SHA256()).hex()
        now = datetime.now(UTC)
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        host = urlsplit(secure_endpoint(url)).hostname
        if (
            not hmac.compare_digest(actual, fingerprint)
            or not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc
            or certificate.issuer != certificate.subject
            or constraints.ca
            or ExtendedKeyUsageOID.SERVER_AUTH not in usage
            or ipaddress.ip_address(host) not in san.get_values_for_type(x509.IPAddress)
        ):
            raise ValueError
        certificate.verify_directly_issued_by(certificate)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=pem)
        return context
    except Exception:
        # A certificate/parser exception can include caller-provided text.
        raise TransportSecurityError() from None


def verify_peer_certificate(der: bytes, fingerprint: str) -> None:
    if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), fingerprint):
        raise TransportSecurityError()
