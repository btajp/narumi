"""TLS without environment-driven session-key logging or trust-store overrides."""

from __future__ import annotations

import ssl
from pathlib import Path


def tls_context() -> ssl.SSLContext:
    # create_default_context() enables SSLKEYLOGFILE before callers can disable it.
    # Constructing the context directly leaves session-key logging disabled.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    paths = ssl.get_default_verify_paths()
    cafile = paths.openssl_cafile
    capath = paths.openssl_capath
    if not cafile or not Path(cafile).is_file():
        cafile = None
    if not capath or not Path(capath).is_dir():
        capath = None
    if cafile is not None or capath is not None:
        # Select the platform/OpenSSL installation's trust locations explicitly;
        # SSL_CERT_FILE and SSL_CERT_DIR belong to no saved provider connection.
        context.load_verify_locations(cafile=cafile, capath=capath)
    # If the installation has no trusted roots, HTTPS fails certificate validation
    # rather than accepting an untrusted peer or consulting an environment override.
    context.set_alpn_protocols(["http/1.1"])
    return context
