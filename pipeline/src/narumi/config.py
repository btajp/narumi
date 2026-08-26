"""Data root resolution and repository-level paths."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "NARUMI_HOME"
ENV_RECORDER = "NARUMI_RECORDER"
DEFAULT_HTTP_PORT = 8765


def data_root(override: str | os.PathLike[str] | None = None) -> Path:
    """Return the data root (``NARUMI_HOME``), creating it if needed.

    Precedence: explicit ``override`` > ``$NARUMI_HOME`` > ``~/Library/Application Support/narumi``.
    """
    if override is not None:
        root = Path(override)
    elif os.environ.get(ENV_HOME):
        root = Path(os.environ[ENV_HOME])
    else:
        root = Path.home() / "Library" / "Application Support" / "narumi"
    root = root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def meetings_root(root: Path | None = None) -> Path:
    base = root if root is not None else data_root()
    path = base / "meetings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def catalog_path(root: Path | None = None) -> Path:
    base = root if root is not None else data_root()
    return base / "narumi.db"


def repo_root() -> Path:
    """Repository root when running from a source checkout (best effort)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "contracts" / "manifest.json").exists():
            return parent
    return here.parents[3]


def contracts_dir() -> Path:
    """Directory holding contract files.

    ``NARUMI_CONTRACTS_DIR`` overrides for installed deployments; otherwise the repo checkout.
    """
    override = os.environ.get("NARUMI_CONTRACTS_DIR")
    if override:
        return Path(override).expanduser()
    return repo_root() / "contracts"
