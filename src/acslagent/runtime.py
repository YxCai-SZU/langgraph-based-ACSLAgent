"""Project-local defaults shared by the installed CLI and the source runner."""
from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """Editable installation follows this checkout when installed in WSL."""
    return Path(__file__).resolve().parents[2]
