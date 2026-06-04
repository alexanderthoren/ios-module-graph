"""Load real per-module compile times captured from the cold build.

Primary source: the Swift compiler's own **`-stats-output-dir`**. `just _index`
builds with that flag, so swift-frontend writes one JSON per source-file invocation
into a stats dir, each carrying its ``time.swift-frontend.*.wall`` seconds, with the
**module name embedded in the filename** (`stats-<ts>-swift-frontend-<Module>-…`).
`aggregate_stats_dir` sums wall-time per module. This is independent of the
`.xcactivitylog` — which on this project never records per-target steps (the build
service's log isn't finalized through the xcsift pipe) — and of xcsift, whose
`--build-info` only recovers durations from the *legacy* build system.

`just _index` aggregates the stats dir into `build_times.json` (a flat
``{module: seconds}`` map) via this module's CLI:

    python3 -m modgraph.build_times <stats-dir> <out.json>

`load_build_times` reads that map; the module name = our SPM target label, and any
module that isn't an SPM target (the app's own sources) folds into the ``app`` node.
Missing/empty → ``{}`` so callers fall back to the type-count proxy. Wall time is
summed across a module's files (work, not wall-clock — files compile in parallel),
which is the right per-module *cost*.

Legacy XCLogParser `flatJson` (list) and xcsift `{"build_info":…}` shapes are still
accepted for robustness.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# swift-frontend stats filename: stats-<ts>-swift-frontend-<Module>-<file>-<arch>-…json
_STATS_NAME = re.compile(r"^stats-\d+-swift-frontend-(.+?)-")


def aggregate_stats_dir(dirpath) -> dict[str, float]:
    """Sum swift-frontend wall seconds per module from a `-stats-output-dir`."""
    d = Path(dirpath)
    if not d.is_dir():
        return {}
    agg: dict[str, float] = defaultdict(float)
    for f in d.glob("stats-*-swift-frontend-*.json"):
        m = _STATS_NAME.match(f.name)
        if not m:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        # Each stats file has one frontend-total wall key for that invocation.
        wall = sum(v for k, v in data.items()
                   if k.startswith("time.swift-frontend.") and k.endswith(".wall"))
        if wall > 0:
            agg[m.group(1)] += wall
    return {k: round(v, 3) for k, v in agg.items()}

# Legacy xcsift duration strings: "234ms", "12.4s", "1m2.3s", "1h3m" (ms before m/s).
_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_duration(text) -> float:
    """Parse a duration string into seconds (0.0 if unparseable)."""
    if not text:
        return 0.0
    secs = 0.0
    for num, unit in _DUR.findall(str(text)):
        v = float(num)
        secs += (v / 1000 if unit == "ms"
                 else v * 3600 if unit == "h"
                 else v * 60 if unit == "m"
                 else v)
    return secs


def _clean_target_name(title) -> str:
    """Normalize an XCLogParser target step title to a bare target name."""
    t = str(title or "").strip()
    if t.startswith("Build target "):
        t = t[len("Build target "):]
    return t.strip().strip("'").strip('"').strip()


def _from_xclogparser(steps) -> dict[str, float]:
    """{target: seconds} from XCLogParser flatJson — target steps that compiled."""
    out: dict[str, float] = {}
    for s in steps:
        if not isinstance(s, dict) or s.get("type") != "target":
            continue
        comp = s.get("compilationDuration") or 0
        if comp <= 0:
            continue  # phases / no-op targets (pre-actions, "Create build request", …)
        name = _clean_target_name(s.get("title") or s.get("signature"))
        if name:
            out[name] = out.get(name, 0.0) + float(comp)
    return out


def _from_xcsift(data) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in (data.get("build_info") or {}).get("targets") or []:
        name = t.get("name")
        secs = parse_duration(t.get("duration"))
        if name and secs > 0:   # durationless targets (modern build system) → ignored
            out[name] = out.get(name, 0.0) + secs
    return out


def load_build_times(path) -> dict[str, float]:
    """Return ``{target_name: seconds}`` from the captured build-times file."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if isinstance(data, list):
        return _from_xclogparser(data)
    if isinstance(data, dict) and "build_info" in data:
        return _from_xcsift(data)
    if isinstance(data, dict):  # pre-aggregated {module: seconds}
        return {k: float(v) for k, v in data.items()
                if isinstance(v, (int, float)) and v > 0}
    return {}


def _main(argv) -> int:
    """CLI: aggregate a -stats-output-dir into a {module: seconds} JSON file."""
    if len(argv) != 2:
        print("usage: python3 -m modgraph.build_times <stats-dir> <out.json>",
              file=sys.stderr)
        return 2
    stats_dir, out = argv
    times = aggregate_stats_dir(stats_dir)
    if not times:
        return 1  # nothing captured — caller leaves no build_times.json
    Path(out).write_text(json.dumps(times, indent=0, sort_keys=True), encoding="utf-8")
    print(f"aggregated {len(times)} module(s), "
          f"{round(sum(times.values()), 1)}s total compile work")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
