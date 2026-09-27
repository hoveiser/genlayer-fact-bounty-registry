"""Constants for the live studionet suite (see ``conftest.py`` for the fixtures).

Kept out of ``conftest.py`` so the module name is unique: pytest registers a
non-package ``conftest.py`` in ``sys.modules`` under the bare name ``conftest``,
and the direct-mode directory uses that same name -- importing it from a test
module would otherwise depend on which directory pytest happened to collect
first.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ATTO_PER_GEN = 10**18

# The single checkable claim type this contract family settles.
CLAIM_REPO = "genlayerlabs/skills"
CLAIM_THRESHOLD = 10
SOURCE_URL = f"https://api.github.com/repos/{CLAIM_REPO}"

# Deadlines are plain UTC ISO-8601 strings; the contract compares the
# fixed-width 'YYYY-MM-DDTHH:MM:SS' prefix lexicographically.
FAR_FUTURE = "2099-12-31T23:59:59"

DEFAULT_REGISTRY = "0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def hex_key(raw) -> str:
    if isinstance(raw, str):
        return raw if raw.startswith("0x") else "0x" + raw
    return "0x" + bytes(raw).hex()
