"""Per-folder extraction scores: what extracting a folder buys vs what it costs.

The migration plan orders folders by graph structure alone; Build mode knows
payoff (blast radius, critical path, churn) but only at module granularity.
This module computes the same payoff lenses **per source folder**, plus an
effort estimate, so consumers (the quick-win ranking, the plan's frontier
ordering, the task list) can surface "small effort, big payoff" steps.

Per folder:

* **warm** — transitive reverse-dependents within the plan's edge set (reusing
  :func:`modgraph.build_impact.compute_build_impact`). One number, two readings:
  the warm-rebuild blast radius extracting it removes, and the *unblocking
  power* — how much of the remaining graph ultimately waits on it. The more
  core a folder, the higher it scores on both.
* **cold** — its own size (declared types) iff it sits on the cold critical
  path; extracting a critical-path folder is what shortens the chain.
  Off-path folders get no cold credit (same stance as
  :mod:`modgraph.build_recommendations`).
* **churn** — commits touching the folder in the window
  (:func:`modgraph.churn.churn_by_folder`); weights payoff by how often the
  cost is actually paid. ``hot`` is ``None`` without churn data.
* **effort** — files to move + outgoing first-party refs to refactor + public
  surface (its types referenced from other folders, which must flip
  ``internal`` -> ``public``; from :func:`modgraph.divide.public_surface` over
  the global ``pair_types``).
* **roi** — churn-weighted payoff over effort: the quick-win sort key.

Normalization mirrors :mod:`modgraph.build_recommendations`:
``combined = 50*warm/maxWarm + 50*cold/maxCold`` (0-100) and
``hot = combined * churn/maxChurn``.

Deterministic: output keyed and computed from sorted iterations only; no
hash-seed-dependent ordering reaches the result.
"""
from __future__ import annotations

from collections import defaultdict

from .build_impact import compute_build_impact
from .churn import churn_by_folder
from .divide import public_surface

_NO_IMPACT = {"warm": 0, "warm_pct": 0.0, "fan_in": 0, "level": 0,
              "crit": False, "scc": 1}


def compute_folder_scores(
    tree: dict,
    plan_edges: dict[tuple[str, str], int],
    source_folders: set[str],
    pair_types: dict[tuple[str, str], set[str]] | None,
    decls: dict[str, set[str]],
    file_records: list[dict],
    churn_commits: list[set[str]] | None = None,
) -> dict:
    """Return ``{"folders": {id: score-dict}, "summary": {...}}``.

    ``plan_edges`` must be the migration plan's edge set (migrated/excluded
    targets already dropped) so warm/cold describe the *remaining* app code.
    ``pair_types`` may be ``None`` (regex-scan fallback): public surface is
    then 0 for every folder and effort degrades to files + refs.
    """
    bi = compute_build_impact(tree, plan_edges)
    impact = bi["nodes"]

    pub = public_surface(pair_types) if pair_types else {}

    files_count: dict[str, int] = defaultdict(int)
    for r in file_records:
        files_count[r["folder"]] += 1

    out_refs: dict[str, int] = defaultdict(int)
    for (a, b), w in plan_edges.items():
        if a != b:
            out_refs[a] += w

    churn_counts = churn_by_folder(churn_commits or [])
    churned = bool(churn_commits)

    rows: dict[str, dict] = {}
    for f in sorted(source_folders):
        met = impact.get(f, _NO_IMPACT)
        cold_score = len(decls.get(f, ())) if met["crit"] else 0
        rows[f] = {
            "warm": met["warm"],
            "fan_in": met["fan_in"],
            "level": met["level"],
            "crit": met["crit"],
            "scc": met["scc"],
            "cold_score": cold_score,
            "churn": churn_counts.get(f, 0),
            "files": files_count.get(f, 0),
            "out_refs": out_refs.get(f, 0),
            "public": len(pub.get(f, ())),
        }

    max_warm = max((r["warm"] for r in rows.values()), default=0) or 1
    max_cold = max((r["cold_score"] for r in rows.values()), default=0) or 1
    max_churn = max((r["churn"] for r in rows.values()), default=0) or 1
    for r in rows.values():
        r["warm_norm"] = round(50.0 * r["warm"] / max_warm, 1)
        r["cold_norm"] = round(50.0 * r["cold_score"] / max_cold, 1)
        r["combined"] = round(r["warm_norm"] + r["cold_norm"], 1)
        r["hot"] = (round(r["combined"] * r["churn"] / max_churn, 1)
                    if churned else None)
        effort = r["files"] + r["out_refs"] + r["public"]
        r["effort"] = effort
        payoff = r["hot"] if r["hot"] is not None else r["combined"]
        r["roi"] = round(payoff / max(effort, 1), 2)

    summary = {
        "folders": len(rows),
        "churned": churned,
        "max_warm": max_warm if rows else 0,
        "max_cold": max_cold if rows else 0,
        "crit_len": bi["summary"]["crit_len"],
    }
    return {"folders": rows, "summary": summary}
