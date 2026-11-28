"""Run packages/visual unit tests under dotfiles' main test runner."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "packages" / "visual" / "tests" / "test_visual.py"
_SPEC = importlib.util.spec_from_file_location("packages_visual_test_visual", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

VisualKitTests = _MOD.VisualKitTests
