#!/usr/bin/env python3
"""Hierarchical Swift module dependency explorer + SPM-migration planner.

Thin entry-point shim. The implementation lives in the ``modgraph`` package
(a sibling of this file). This script is kept so the documented invocation and
the ``justfile`` recipes keep working unchanged:

    python3 find_leaf_modules.py <project_root> [--graph PATH] [--list PATH] ...

Run ``python3 find_leaf_modules.py --help`` for the full option list, or see
``modgraph/__init__.py`` for the module map. Equivalent: ``python3 -m modgraph``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when invoked by path from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from modgraph.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
