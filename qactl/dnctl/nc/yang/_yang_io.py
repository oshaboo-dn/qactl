"""Tiny filesystem helpers for reading cached YANG modules."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from qactl.dnctl.core import paths as _paths

ROOT_DIR = _paths.state_dir("nc")
YANGS_DIR = ROOT_DIR / "yangs"


def yang_store_path(build: str, sub_build: str = "") -> Path:
    """Return ``yangs/<build>/<sub_build>/`` path (no existence check)."""
    base = YANGS_DIR / build
    return base / sub_build if sub_build else base


@lru_cache(maxsize=256)
def read_yang_source(
    build: str, sub_build: str, module_name: str,
) -> Optional[str]:
    """Return the raw YANG source for ``module_name`` or ``None`` if absent."""
    filepath = yang_store_path(build, sub_build) / f"{module_name}.yang"
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return None
