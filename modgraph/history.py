"""Append-only build-cost history so successive extractions can be compared.

The workflow this serves: extract a module → ``just clean`` → ``just tree`` →
check the build got cheaper, repeat. Each full render auto-records ONE snapshot —
keyed to the **target project's** git commit — into a JSONL log that deliberately
**survives ``just clean``** (the whole point: track improvement *across* runs, not
within one). Build mode's "Improvements" tab visualizes the series.

Two classes of metric live in each row, and they are NOT equal:

* **structural** (module/edge/cycle counts, warm blast radius, critical-path
  depth) — deterministic. Same code ⇒ same numbers. This is the *honest*
  improvement signal: extract a module and these move for real.
* **wall** (``est_wall_s``, ``total_build_s``) — measured, hence noisy (thermal
  throttle, background load, cache state). Directional, not proof. A single
  before/after delta can be smaller than the run-to-run noise.

Re-rendering the same state is **deduped** (fingerprint of sha + the metrics that
matter), so running ``just tree`` twice on one commit doesn't grow the log — you
get one row per real change. Best-effort throughout: a missing git, an unwritable
file, or a malformed line never breaks a render.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _git(project_dir, *args) -> str:
    """Run a git command in the target project; '' on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _git_state(project_dir):
    """(sha, commit-subject, dirty?) for the target project, all best-effort."""
    sha = _git(project_dir, "rev-parse", "HEAD")
    if not sha:
        return "", "", False
    subject = _git(project_dir, "log", "-1", "--pretty=%s")
    dirty = bool(_git(project_dir, "status", "--porcelain"))
    return sha, subject, dirty


def build_snapshot(module_graph, plan, project_dir, *, captured_at=None) -> dict:
    """Assemble one history row from a freshly computed module graph + plan."""
    nodes = module_graph.get("nodes", [])
    summary = module_graph.get("summary", {})
    sha, subject, dirty = _git_state(project_dir)
    warm = [n.get("warm", 0) for n in nodes]
    return {
        "captured_at": captured_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha": sha,
        "sha_short": sha[:7],
        "dirty": dirty,
        "subject": subject,
        # Measured, noisy — directional only.
        "wall": {
            "est_wall_s": summary.get("est_wall_s", 0.0),
            "total_build_s": summary.get("total_build_s", 0.0),
            "cores": summary.get("cores", 0),
            "measured": bool(summary.get("measured", False)),
        },
        # Deterministic — the real improvement signal.
        "structural": {
            "modules": len(nodes),
            "spm": sum(1 for n in nodes if n.get("kind") == "spm"),
            "app": sum(1 for n in nodes if n.get("kind") == "app"),
            "edges": len(module_graph.get("edges", [])),
            "cycles": summary.get("n_cycles", 0),
            "crit_len": summary.get("crit_len", 0),
            "max_width": summary.get("max_width", 0),
            "warm_max": max(warm, default=0),
            "warm_total": sum(warm),
            "types": sum(n.get("types", 0) for n in nodes),
        },
        "plan": {
            "steps": len(plan),
            "cyclic_bundles": sum(1 for s in plan if s.get("is_cycle")),
        },
    }


def _dedup_key(snap):
    """Fingerprint that changes iff a meaningful metric (or the commit) did."""
    s, w = snap["structural"], snap["wall"]
    return (
        snap["sha"], w["est_wall_s"], w["total_build_s"],
        s["modules"], s["edges"], s["cycles"], s["warm_total"],
        snap["plan"]["steps"],
    )


def load_history(path) -> list:
    """Read the JSONL history into a list (oldest→newest); skip malformed lines."""
    p = Path(path)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def append_snapshot(path, module_graph, plan, project_dir, *, captured_at=None):
    """Append one snapshot unless it duplicates the last recorded state.

    Returns the written snapshot, or ``None`` when deduped (the last row already
    has the same fingerprint). Dedup keeps the log to one row per real change so
    re-running the renderer on an unchanged commit is a no-op.
    """
    snap = build_snapshot(module_graph, plan, project_dir, captured_at=captured_at)
    existing = load_history(path)
    if existing and _dedup_key(existing[-1]) == _dedup_key(snap):
        return None
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap) + "\n")
    return snap
