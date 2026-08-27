"""Optional gaia-library integration (相棒の記憶索引 MCP).

gaia-library is an *optional* companion (AGENTS.md): narumi is complete without it and more
precise with it. When ``NARUMI_GAIA_URL`` is set, the pipeline queries it while building the
meeting brief (``narumi.brief``) and proposes updates after export (``narumi.export.gaia``).
Writes go through ``propose_update`` only — the proposal queue with human approval on the
gaia-library side (絶対原則 5); there is no privileged write path.
"""

from narumi.gaia.client import ENV_GAIA_URL, GaiaClient

__all__ = ["ENV_GAIA_URL", "GaiaClient"]
