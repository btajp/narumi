"""Derive the base64 Ed25519 public key (SUPublicEDKey) from a Sparkle private key file.

The input is the "new format" Sparkle private key file: base64 of a 32-byte Ed25519 seed
(the same format `generate_keys -x` exports and `sign_update --ed-key-file` reads).

Usage: uv run --no-project --with cryptography python derive-pubkey.py <seed-file>
"""

import base64
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <seed-file>")
    with open(sys.argv[1], encoding="ascii") as f:
        seed = base64.b64decode(f.read().strip())
    if len(seed) != 32:
        raise SystemExit(f"seed must be 32 bytes after base64 decoding, got {len(seed)}")
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    print(base64.b64encode(public_raw).decode("ascii"))


if __name__ == "__main__":
    main()
