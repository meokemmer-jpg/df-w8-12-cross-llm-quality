# DF-W8-12 pytest conftest [CRUX-MK]
"""
Pytest-Configuration: src auf sys.path setzen.
"""
from __future__ import annotations

import sys
from pathlib import Path

# tests/ -> df-w8-12-cross-llm-quality/ -> src einhaengen
_pkg_root = Path(__file__).resolve().parent.parent
_src_parent = _pkg_root  # src ist Subdirectory
if str(_src_parent) not in sys.path:
    sys.path.insert(0, str(_src_parent))
