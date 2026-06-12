"""Render the master plan as markdown / JSON — the CLI's Plan artifact.

``migration_plan.md`` is the file-shaped twin of the HTML's Plan tab: both
read the same :func:`modgraph.master_plan.compute_master_plan` output, so the
CLI and the UI can never disagree on order or content. The markdown carries
the Setup checklist, the phase-grouped steps (each with its shape decision,
build-grounded why, what moves, and the verify block), the stop line with the
deferred items and their reasons, and the equilibrium criteria.
"""
from __future__ import annotations

import json
from pathlib import Path

from .master_plan import PHASE_LABELS


def _shape_line(step: dict) -> str:
    sh = step.get("shape") or {}
    mode = sh.get("mode", "")
    if mode == "api_impl":
        body = (f"ships as `{sh.get('api_module')}` (API: protocols + value "
                f"types) + `{sh.get('impl_module')}` (implementation, bound "
                f"at the composition root)")
    elif mode == "single_module":
        body = f"ships as the single module `{sh.get('impl_module')}`"
    elif mode == "absorb":
        body = f"absorb into the existing module `{sh.get('destination')}`"
    elif mode == "move_file":
        body = f"move the file to `{sh.get('destination')}/`"
    elif mode == "api_retrofit":
        body = (f"add `{sh.get('api_module')}` (protocols + value types) in "
                f"front of the existing `{sh.get('impl_module')}`; rewire "
                f"consumers to the API, bind the impl at the composition root")
    elif mode == "isolate":
        body = (f"pull the type (and its drag closure) into the new module "
                f"`{sh.get('impl_module')}`")
    elif mode == "split":
        body = "split along the unit boundaries (see the Divide view)"
    elif mode == "partition":
        body = (f"split `{sh.get('impl_module')}` along its usage seams — "
                f"one slice per consumer cohort + a shared core")
    elif mode == "join":
        body = f"fold into `{sh.get('destination')}`"
    else:
        body = mode
    rule = sh.get("rule")
    return f"- **Shape:** `{mode}` — {body}" + (f" — {rule}" if rule else "")


def _why_line(step: dict) -> list[str]:
    why = step.get("why") or {}
    bits = []
    if why.get("roi") is not None:
        bits.append(f"roi {why['roi']} (payoff {why.get('payoff')} ÷ effort "
                    f"{why.get('effort')})")
    if why.get("churn"):
        bits.append(f"churn {why['churn']} commit(s)")
    if why.get("crit"):
        bits.append("on the cold critical path")
    if why.get("cold_delta_s"):
        bits.append(f"~{why['cold_delta_s']}s compile work leaves the app "
                    f"target (estimated)")
    out = [f"- **Why:** {why.get('narrative', '')}"]
    if bits:
        out.append(f"  ({' · '.join(bits)})")
    return out


def _step_lines(num: int, step: dict) -> list[str]:
    sh = step.get("shape") or {}
    what = step.get("what") or {}
    verify = step.get("verify") or {}
    lines = [f"### Step {num} — {step['title']}", ""]
    lines.append(_shape_line(step))
    lines.extend(_why_line(step))
    moves = []
    if what.get("files"):
        moves.append(f"{what['files']} file(s)")
    if what.get("types"):
        moves.append(f"{what['types']} type(s)")
    if what.get("resources_count"):
        sample = ", ".join(what.get("resources") or [])
        more = what["resources_count"] - len(what.get("resources") or [])
        moves.append(f"{what['resources_count']} resource(s)"
                     + (f" ({sample}{', …' if more > 0 else ''})" if sample else ""))
    if moves:
        lines.append(f"- **Moves:** {' · '.join(moves)}")
    if sh.get("api_surface"):
        more = sh.get("api_surface_count", 0) - len(sh["api_surface"])
        lines.append(f"- **API surface ({sh.get('api_surface_count')}):** "
                     + ", ".join(f"`{t}`" for t in sh["api_surface"])
                     + (f", … ({more} more)" if more > 0 else ""))
        if sh.get("protocols_for"):
            lines.append("  - protocols for (reference types with behavior): "
                         + ", ".join(f"`{t}`" for t in sh["protocols_for"]))
    parts = (step.get("details") or {}).get("parts") or []
    if parts:
        core = (step.get("details") or {}).get("core") or {}
        lines.append(f"- **Slices** (+ shared core of {core.get('types', 0)} "
                     f"type(s), {core.get('share_pct', 0)}%):")
        for p in parts:
            sample = ", ".join(f"`{t}`" for t in p.get("type_sample", [])[:4])
            lines.append(f"  - serves {', '.join(p.get('consumers', []))} — "
                         f"{p['types']} type(s), {p['public']} public, "
                         f"{p['ext_refs']} external ref(s)"
                         + (f" ({sample}, …)" if sample else ""))
    if step.get("after"):
        lines.append("- **After:** " + ", ".join(f"`{a}`" for a in step["after"]))
    if step.get("unblocks"):
        lines.append("- **Unblocks:** "
                     + ", ".join(f"`{u}`" for u in step["unblocks"]))
    alt = (step.get("details") or {}).get("alternative")
    if alt:
        lines.append(f"- **Alternative:** {alt}")
    cmds = verify.get("commands") or []
    exp = verify.get("expect") or {}
    if cmds or exp:
        lines.append("- **Verify:**")
        for c in cmds:
            lines.append(f"  - `{c}`")
        if exp:
            lines.append("  - expect: "
                         + " · ".join(f"{k} {v}" for k, v in sorted(exp.items())))
    lines.append("")
    return lines


def master_plan_markdown(master_plan: dict, meta: dict) -> str:
    """The whole plan as one markdown document (see module docstring)."""
    mp = master_plan or {}
    steps = mp.get("steps", [])
    setup = mp.get("setup", [])
    deferred = mp.get("deferred", [])
    eq = mp.get("equilibrium") or {"criteria": [], "met": False}
    summary = mp.get("summary") or {}

    lines: list[str] = []
    lines.append(f"# Migration Plan — {meta['root_label']}")
    lines.append("")
    lines.append(f"Project root: `{meta['root_path']}`  ")
    lines.append(f"Folders already in SPM (baseline): **{meta['baseline_count']}**"
                 f" — prefixes: `{', '.join(meta['migrated_prefixes']) or '(none)'}`  ")
    excluded_count = meta.get("excluded_count", 0)
    blocked_reasons = meta.get("blocked_reasons", {}) or {}
    if excluded_count:
        lines.append(f"Folders marked won't-be-modularized (excluded): "
                     f"**{excluded_count}**  ")
    if blocked_reasons:
        lines.append(f"Folders that can't be modularized (depend on excluded): "
                     f"**{len(blocked_reasons)}**  ")
    n_phases = len({s["phase"] for s in steps})
    lines.append(f"Steps: **{len(steps)}** in **{n_phases}** phase(s) · "
                 f"**{len(deferred)}** deferred below the stop line · "
                 f"equilibrium **{'MET' if eq.get('met') else 'not met'}**")
    lines.append("")

    if setup:
        lines.append("## Setup (one-time, do these first)")
        lines.append("")
        for it in setup:
            lines.append(f"- [ ] **{it['title']}** — {it['why']}")
            for i, h in enumerate(it.get("how", []), 1):
                lines.append(f"  {i}. {h}")
            if it.get("done_when"):
                lines.append(f"  - Done when: {it['done_when']}")
        lines.append("")

    lines.append("## How to use this plan")
    lines.append("")
    lines.append(
        "Each step is one behavior-preserving PR — move code, adjust access "
        "levels, rewire imports, introduce protocols; never edit logic. "
        "Phases respect dependencies (a step's `After` list names its "
        "prerequisites); within a phase, best payoff-per-effort comes first. "
        "Steps shaped `api_impl` ship as an API + implementation pair: "
        "consumers import the API package only, the implementation is bound "
        "at the composition root. After each step ships, run `just refresh` "
        "for a structural check (and `just clean && just tree` periodically "
        "for measured build times), then regenerate this plan so the next "
        "step reflects the new state. Stop at the stop line: below it, "
        "effort outweighs payoff."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    current_phase = None
    for i, s in enumerate(steps, 1):
        if s["phase"] != current_phase:
            current_phase = s["phase"]
            n_in = sum(1 for x in steps if x["phase"] == current_phase)
            label = PHASE_LABELS.get(current_phase, f"Phase {current_phase}")
            lines.append(f"## Phase {current_phase} — {label} ({n_in} step(s))")
            lines.append("")
        lines.extend(_step_lines(i, s))

    lines.append("---")
    lines.append("")
    lines.append(f"## 🛑 Stop line")
    lines.append("")
    lines.append(summary.get("stop", ""))
    lines.append("")
    if deferred:
        lines.append(f"### Deferred ({len(deferred)}) — looked at, set aside")
        lines.append("")
        for d in deferred:
            lines.append(f"- **{d['title']}** — `{d['reason']}`: {d['why']}")
        lines.append("")

    traj = mp.get("trajectory") or {}
    rows = traj.get("steps") or []
    if rows:
        base = traj.get("baseline") or {}
        unit = traj.get("unit", "types")
        lines.append("## Projected trajectory (simulated, cumulative)")
        lines.append("")
        lines.append(f"Costs are in {unit}-units (structural proxy — measured "
                     f"seconds can't be attributed to modules that don't "
                     f"exist yet). Each row assumes every step above it "
                     f"already shipped.")
        lines.append("")
        lines.append("| step | modules | crit len | warm max | warm cost | "
                     "cold chain | app share |")
        lines.append("|---|---|---|---|---|---|---|")
        lines.append(f"| (baseline) | {base.get('modules')} | "
                     f"{base.get('crit_len')} | {base.get('warm_max')} | "
                     f"{base.get('warm_cost')} | {base.get('cold_cost')} | "
                     f"{base.get('app_share_pct')}% |")
        for r in rows:
            tag = "" if r.get("simulated") else " *(not simulated)*"
            lines.append(f"| `{r['id']}`{tag} | {r.get('modules')} | "
                         f"{r.get('crit_len')} | {r.get('warm_max')} | "
                         f"{r.get('warm_cost')} | {r.get('cold_cost')} | "
                         f"{r.get('app_share_pct')}% |")
        lines.append("")

    lines.append("## Equilibrium — the definition of done")
    lines.append("")
    for c in eq.get("criteria", []):
        mark = "x" if c.get("met") else " "
        lines.append(f"- [{mark}] **{c['label']}** — {c['current']} "
                     f"(target: {c['target']})")
    proj = eq.get("projected") or {}
    if proj:
        lines.append("")
        lines.append(f"Projected end state if every simulated step ships: "
                     f"{proj.get('modules')} module(s), app share "
                     f"{proj.get('app_share_pct')}%, worst warm radius "
                     f"{proj.get('warm_max_pct')}%, {proj.get('n_cycles')} "
                     f"module cycle(s).")
    lines.append("")

    if blocked_reasons:
        lines.append("## Can't modularize (blocked by won't-modularize)")
        lines.append("")
        lines.append(
            "These folders are out of the plan because they transitively "
            "depend on a folder marked won't-modularize. An SPM package "
            "cannot link against non-SPM app-target code, so they stay put "
            "until the blocking dependency is severed (or the blocker is "
            "unmarked)."
        )
        lines.append("")
        for folder in sorted(blocked_reasons):
            blockers = ", ".join(f"`{b}`" for b in blocked_reasons[folder])
            lines.append(f"- `{folder}` — blocked by: {blockers}")
        lines.append("")

    return "\n".join(lines)


def write_master_plan_markdown(master_plan: dict, meta: dict,
                               out_path: Path) -> None:
    out_path.write_text(master_plan_markdown(master_plan, meta),
                        encoding="utf-8")


def write_master_plan_json(master_plan: dict, meta: dict,
                           out_path: Path) -> None:
    out_path.write_text(
        json.dumps({"meta": meta, "master_plan": master_plan}, indent=2),
        encoding="utf-8",
    )
