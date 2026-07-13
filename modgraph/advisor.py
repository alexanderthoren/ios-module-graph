"""Unified modularization advisor: one ordered feed, one decision per subject.

The pipeline computes many *independent* advice streams — misplaced files
(:mod:`modgraph.file_affinity`), per-folder quick wins
(:mod:`modgraph.quick_wins`), type isolations (:mod:`modgraph.isolate`),
composite-module splits (:mod:`modgraph.module_splits`), build-payoff split
rankings (:mod:`modgraph.build_recommendations`), division plans
(:mod:`modgraph.divide`). Each is right in its own lane, but a user opening the
graph sees six competing surfaces and no answer to the only questions a
modularization actually runs on: **what do I do next, in what order, and when
do I stop?**

This module is the arbiter. It consumes the streams the pipeline already
computed (no recompute, no new graph walks beyond the module-graph join scan)
and emits ONE plan:

* **Waves** — actions sequenced by dependency, not by which engine found them:

  0. *file moves* — the smallest PRs, dissolve fake coupling first, but only
     when the move sits on a boundary the plan uses (crosses a compile unit,
     feeds a planned cut, or touches an extraction candidate); a same-module
     tidy is deferred ``no_boundary``;
  1. *ready extractions* — quick wins extractable today, ROI order;
  2. *unblocked extractions* — folders whose whole cut-set is move-file
     fixable, cross-linked ``after`` the wave-0 move ids that unblock them,
     plus cut-first folders whose cut is small enough to be worth the surgery;
  3. *module surgery* — splits / isolations / divisions, ONE decision per
     module (see below), ordered by build-cost leverage;
  4. *joins* — a tiny SPM module with exactly one SPM consumer is boundary
     without benefit; fold it into its consumer. The inverse operation of
     everything above — and the reason the advisor can say "stop":
     modularization is done when neither direction pays.

* **One decision per module** — when module-splits, isolations and division
  plans all target the same module, the advisor picks one (trapped low-level
  core → split; one type carrying ``ADVISOR_ISOLATE_SHARE`` % of the external
  fan-in → isolate; otherwise the division plan) and records the runner-up as
  ``alternative`` instead of letting tabs compete for the same subject.

* **A stop line** — actions whose payoff falls under ``ADVISOR_TAIL_PCT`` % of
  their kind's best are *deferred with a reason*, not ranked at position 41.
  The feed ends where effort stops paying; every ``deferred`` row says why
  (``tail`` / ``blocked_cut`` / ``no_boundary`` / ``stabilize_api`` /
  ``unused``) so the stop is auditable, mirroring the quick-win
  REJECT-is-first-class stance.

Pure interpretation over already-computed dicts; deterministic (sorted
iteration only); every stream is optional (regex-scan path → quick wins only,
classification already degraded upstream). Ships as ``payload["advice"]``.
"""
from __future__ import annotations

from collections import defaultdict

from .config import (ADVISOR_CUT_MAX, ADVISOR_ISOLATE_SHARE,
                     ADVISOR_JOIN_MAX_TYPES, ADVISOR_TAIL_PCT,
                     API_MIN_CONSUMERS)
from .module_graph import APP_ID, module_of

WAVE_LABELS = {
    0: "Move misplaced files",
    1: "Extract what's ready",
    2: "Unblock and extract",
    3: "Module surgery",
    4: "Fold needless boundaries",
}


def _action(aid: str, kind: str, wave: int, subject: str, title: str,
            why: str, payoff: float, effort: float, roi: float | None,
            **details) -> dict:
    return {
        "id": aid, "kind": kind, "wave": wave, "subject": subject,
        "title": title, "why": why,
        "payoff": payoff, "effort": effort, "roi": roi,
        "after": [], "unblocks": [], "details": details,
    }


def _deferred(aid: str, kind: str, subject: str, title: str,
              reason: str, why: str) -> dict:
    return {"id": aid, "kind": kind, "subject": subject, "title": title,
            "reason": reason, "why": why}


def _cut_mix(cut_edges: list[dict]) -> str:
    """Human summary of a cut-set's fix composition: "2 file move(s), 1 inversion"."""
    counts: dict[str, int] = defaultdict(int)
    for e in cut_edges:
        counts[e["fix"]] += 1
    names = {"move_file": "file move(s)", "shared_primitive":
             "shared-primitive push-down(s)", "invert": "inversion(s)"}
    return ", ".join(f"{counts[k]} {names[k]}" for k in
                     ("move_file", "shared_primitive", "invert") if counts[k])


def compute_advice(quick_wins: dict, file_moves: dict,
                   isolations: dict[str, dict], module_splits: dict,
                   recommendations: dict, module_graph: dict,
                   partitions: dict[str, dict] | None = None,
                   migrated_prefixes: list[str] | None = None) -> dict:
    """Return ``{"actions": [...], "deferred": [...], "summary": {...}}``.

    ``actions`` is the ordered do-this-next feed (wave-major, see module doc);
    ``deferred`` is everything the engine looked at and set aside, each with a
    machine ``reason`` and a human ``why`` — the stop line is the boundary
    between the two lists. All inputs are the payload dicts the pipeline
    already computed; any of them may be empty.
    """
    qw_items = (quick_wins or {}).get("items", [])
    fm_items = (file_moves or {}).get("items", [])
    isolations = isolations or {}
    partitions = partitions or {}
    ms_items = (module_splits or {}).get("items", [])
    reco_items = (recommendations or {}).get("items", [])
    nodes = (module_graph or {}).get("nodes", [])
    edges = (module_graph or {}).get("edges", [])

    actions: list[dict] = []
    deferred: list[dict] = []

    # The tail floor is relative to the best quick win: below it, the payoff
    # no longer justifies queue position — deferred, with the numbers shown.
    # Computed here (ahead of wave 0) because the boundary gate needs it too.
    max_payoff = max((i.get("payoff") or 0 for i in qw_items), default=0)
    tail_floor = max_payoff * ADVISOR_TAIL_PCT / 100.0

    # ---- wave 0: misplaced files, boundary-gated ----------------------------
    # A move only earns a wave-0 slot when it dissolves coupling on a boundary
    # the plan actually uses. The plan's boundaries are the modules that exist
    # today (migrated prefixes) PLUS the folders it will extract (quick wins
    # above the tail floor) — each acts as its own module via longest-prefix
    # routing. A file shuffled between two folders that land in the SAME
    # planned module, and that no planned cut needs, is an intra-module tidy
    # with no build payoff: deferred ``no_boundary``, not ranked. This is the
    # PlanDetail/State → PlanDetail/Reducers case — pointless while PlanDetail
    # extracts whole, real once the plan splits them apart. Gate active only
    # when the caller supplies the prefix set (the real pipeline); a bare
    # advisor unit test passes None and every move is kept.
    cut_evidence = {p for it in qw_items
                    for e in it.get("cut", {}).get("edges", [])
                    for p in e.get("evidence", ())}
    planned_prefixes = (list(migrated_prefixes or [])
                        + [it["folder"] for it in qw_items
                           if (it.get("payoff") or 0) >= tail_floor])
    move_actions: dict[str, dict] = {}  # file path -> action (for after-links)
    for it in fm_items:
        src, dst = it["from"], it["to"]
        if migrated_prefixes is not None:
            src_mod = module_of(src, planned_prefixes)
            dst_mod = module_of(dst, planned_prefixes)
            # in_cut keeps moves the wave-2 cross-linking depends on — those
            # edges are exactly a planned extraction's blockers.
            if src_mod == dst_mod and it["file"] not in cut_evidence:
                deferred.append(_deferred(
                    f"move:{it['file']}", "move_file", it["file"],
                    f"Move {it['file'].rsplit('/', 1)[-1]} → {dst}/",
                    "no_boundary",
                    f"{src} and {dst} both land in {src_mod} under the plan "
                    f"and no planned cut crosses this edge — an intra-module "
                    f"tidy with no build payoff. Revisit if {src_mod} is split "
                    f"or one side becomes a separate extraction."))
                continue
        a = _action(
            f"move:{it['file']}", "move_file", 0, it["file"],
            f"Move {it['file'].rsplit('/', 1)[-1]} → {it['to']}/",
            f"{it['refs']} of its references bind to {it['to']} vs "
            f"{it['own_refs']} at home — the move dissolves fake coupling.",
            payoff=float(it["refs"]), effort=1, roi=float(it["refs"]),
            file=it["file"], src=it["from"], dst=it["to"],
        )
        move_actions[it["file"]] = a
        actions.append(a)

    # ---- waves 1-2: quick wins (input already ROI-sorted; order preserved) --
    wave2: list[tuple[int, int, dict]] = []  # (blocked?, input idx, action)

    for idx, it in enumerate(qw_items):
        f = it["folder"]
        payoff = it.get("payoff") or 0
        alt = None
        if f in isolations:
            top = isolations[f]["candidates"][0]
            share = isolations[f]["summary"]["top_share"]
            alt = (f"or isolate {top['type']} ({share}% of the external "
                   f"fan-in) instead of moving the whole folder")
        if it["extractable_now"]:
            dest = it.get("destination")
            if payoff < tail_floor:
                deferred.append(_deferred(
                    f"qw:{f}", it["action"], f, f"Extract {f}", "tail",
                    f"payoff {payoff} is under {ADVISOR_TAIL_PCT}% of the "
                    f"best ({max_payoff}) — effort is better spent above "
                    f"the line."))
                continue
            if dest:
                title = f"Absorb {f} into {dest['label']}"
                why = (f"Extractable today; {dest['refs']} shared ref(s) with "
                       f"{dest['label']} (L{dest['level']}) and the landing is "
                       f"level-preserving — no new manifest needed.")
            else:
                title = f"Extract {f} as a new module"
                why = (f"Extractable today; no existing module fits, lands at "
                       f"L{it['landing_level']}.")
            a = _action(f"qw:{f}", it["action"], 1, f, title, why,
                        payoff=payoff, effort=it.get("effort", 0),
                        roi=it.get("roi", 0.0), folder=f)
            if alt:
                a["details"]["alternative"] = alt
            actions.append(a)
            continue

        # cut-first: worth surgery when the payoff clears the tail floor AND
        # either every blocking edge is a file move we can sequence after
        # wave 0, or the total cut is small enough to do by hand.
        cut = it["cut"]
        cut_edges = cut["edges"]
        if payoff < tail_floor:
            deferred.append(_deferred(
                f"qw:{f}", "cut_then_extract", f, f"Extract {f}", "tail",
                f"payoff {payoff} is under {ADVISOR_TAIL_PCT}% of the best "
                f"({max_payoff}) — not worth the {cut['total_refs']}-ref cut."))
            continue
        movable = bool(cut_edges) and all(e["fix"] == "move_file"
                                          for e in cut_edges)
        after: list[str] = []
        if movable:
            evidence = sorted({p for e in cut_edges for p in e["evidence"]})
            if all(p in move_actions for p in evidence):
                after = [move_actions[p]["id"] for p in evidence]
        if after or cut["total_refs"] <= ADVISOR_CUT_MAX:
            if after:
                title = f"Move {len(after)} file(s), then extract {f}"
                why = (f"Blocked only by {len(after)} misplaced file(s) "
                       f"already queued in wave 0 — extract right after "
                       f"they move.")
            else:
                title = f"Cut {cut['total_refs']} ref(s), then extract {f}"
                why = (f"{len(cut_edges)} blocking edge(s), "
                       f"{cut['total_refs']} ref(s) to cut "
                       f"({_cut_mix(cut_edges)}) — small enough to be worth "
                       f"the payoff.")
            a = _action(f"qw:{f}", "cut_then_extract", 2, f, title,
                        why, payoff=payoff, effort=it.get("effort", 0),
                        roi=it.get("roi", 0.0), folder=f,
                        cut_refs=cut["total_refs"])
            a["after"] = after
            if alt:
                a["details"]["alternative"] = alt
            for aid in after:
                # mirror link: the move knows what it unblocks.
                for m in move_actions.values():
                    if m["id"] == aid and f not in m["unblocks"]:
                        m["unblocks"].append(f)
            wave2.append((0 if after else 1, idx, a))
        else:
            deferred.append(_deferred(
                f"qw:{f}", "cut_then_extract", f, f"Extract {f}",
                "blocked_cut",
                f"{len(cut_edges)} blocking edge(s), {cut['total_refs']} "
                f"ref(s) ({_cut_mix(cut_edges)}) — bigger surgery than the "
                f"wins above; revisit after the board clears."))

    wave2.sort(key=lambda t: (t[0], t[1]))
    actions.extend(a for _b, _i, a in wave2)
    emitted_folders = {a["subject"] for a in actions if a["wave"] in (1, 2)}

    # ---- wave 3: module surgery — one decision per module -------------------
    spm_nodes = {n["id"]: n for n in nodes if n.get("kind") == "spm"}
    # Direct (not transitive) dependents over the module graph: an edge
    # from -> to means "from depends on to". Used by the API-retrofit gate
    # here and the join wave below.
    direct_deps: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        direct_deps[e["to"]].add(e["from"])
    reco_by_id = {r["id"]: r for r in reco_items}
    split_by_id = {s["module"]: s for s in ms_items}
    subjects = sorted((set(split_by_id) | set(isolations) | set(partitions) |
                       {r["id"] for r in reco_items if r["kind"] == "spm"})
                      - {APP_ID})
    max_leverage = max(((r.get("hot") if r.get("hot") is not None
                         else r.get("combined", 0)) for r in reco_items
                        if r["kind"] == "spm"), default=0)
    surgery_tail = max_leverage * ADVISOR_TAIL_PCT / 100.0
    wave3: list[tuple[float, float, str, dict]] = []
    surgery_modules: set[str] = set()
    ignored_tail = 0

    def _isolate_action(mid: str, iso: dict) -> dict:
        top = iso["candidates"][0]
        share = iso["summary"]["top_share"]
        return _action(
            f"mod:{mid}", "isolate_type", 3, mid,
            f"Isolate {top['type']} out of {mid}",
            f"{top['type']} carries {share}% of the module's external fan-in "
            f"({top['ext_refs']} ref(s) from {top['ext_modules']} module(s)) "
            f"and drags {top['pulls_total']} type(s) — outside consumers can "
            f"depend on the small new module instead.",
            payoff=float(top["module_ext_refs"]),
            effort=top["module_size"] + top["module_public"],
            roi=top["roi"], module=mid, type=top["type"])

    # Isolations get their own ROI-relative tail: a seed whose drag closure
    # outweighs the fan-in it frees should not ride the feed on share alone.
    iso_best_roi = max((i["candidates"][0]["roi"]
                        for i in isolations.values()), default=0.0)
    iso_floor = iso_best_roi * ADVISOR_TAIL_PCT / 100.0

    def _iso_or_defer(mid: str, iso: dict, leverage: float,
                      native: float) -> None:
        top = iso["candidates"][0]
        if top["roi"] < iso_floor:
            deferred.append(_deferred(
                f"mod:{mid}", "isolate_type", mid,
                f"Isolate {top['type']} out of {mid}", "tail",
                f"isolation roi {top['roi']} is under {ADVISOR_TAIL_PCT}% of "
                f"the best ({iso_best_roi}) — the closure it drags outweighs "
                f"the fan-in it frees, for now."))
            return
        wave3.append((leverage, native, mid, _isolate_action(mid, iso)))
        surgery_modules.add(mid)

    for mid in subjects:
        split_it = split_by_id.get(mid)
        iso = isolations.get(mid)
        reco = reco_by_id.get(mid)
        leverage = 0.0
        if reco:
            leverage = (reco["hot"] if reco.get("hot") is not None
                        else reco.get("combined", 0))
        label = (reco or {}).get("label") or spm_nodes.get(mid, {}).get(
            "label", mid)

        if mid not in spm_nodes:
            # An app subtree (not yet a module). Its isolation is an
            # alternative to the folder's own quick win when one was emitted.
            if iso and mid not in emitted_folders:
                _iso_or_defer(mid, iso, 0.0, float(iso["ext_total"]))
            continue

        # A split with no releasable consumer frees nobody — fall through to
        # the isolation/division branches instead of shipping a no-op action.
        if split_it and split_it["releasable"]:
            a = _action(
                f"mod:{mid}", "split_module", 3, mid,
                f"Split the L{split_it['min_intrinsic']} core out of {label}",
                f"L{split_it['level']} module hides an "
                f"L{split_it['min_intrinsic']} core "
                f"({len(split_it['low_units'])} unit(s)); "
                f"{len(split_it['releasable'])} consumer(s) could drop their "
                f"dependency height; {split_it['public_cost']} type(s) go "
                f"public.",
                payoff=float(split_it["score"]),
                effort=split_it["public_cost"], roi=None,
                module=mid, low_units=split_it["low_units"])
            if iso:
                top = iso["candidates"][0]
                a["details"]["alternative"] = (
                    f"or isolate {top['type']} "
                    f"({iso['summary']['top_share']}% of external fan-in)")
            wave3.append((leverage, float(split_it["score"]), mid, a))
            surgery_modules.add(mid)
        elif iso and iso["summary"]["top_share"] >= ADVISOR_ISOLATE_SHARE:
            _iso_or_defer(mid, iso, leverage,
                          float(iso["candidates"][0]["roi"]))
        elif (part := partitions.get(mid)) and part["verdict"] == "ok" \
                and leverage >= surgery_tail:
            # Usage-cohort partition: split along how consumers actually use
            # the module, priced against the module graph. Preferred over the
            # folder-based division — folders are a proxy, closures are not.
            sim = part.get("sim") or {}
            freed = sum(p["ext_refs"] for p in part["parts"])
            pub = sum(p["public"] for p in part["parts"])
            n_parts = len(part["parts"])
            why = (f"{part['summary']['consumers']} consumer boundar(ies) "
                   f"pull {n_parts} disjoint slice(s) "
                   f"({freed} external ref(s)); the shared core keeps "
                   f"{part['core']['share_pct']}% of the module.")
            if sim.get("warm_delta") is not None:
                why += (f" Simulated churn-weighted rebuild "
                        f"{sim['warm_cost']} {sim.get('unit', 'types')}-units.")
            a = _action(
                f"mod:{mid}", "partition_module", 3, mid,
                f"Partition {label} into {n_parts} usage slice(s) + core",
                why, payoff=float(freed), effort=float(pub), roi=None,
                module=mid, parts=n_parts)
            if iso:
                top = iso["candidates"][0]
                a["details"]["alternative"] = (
                    f"or isolate {top['type']} "
                    f"({iso['summary']['top_share']}% of external fan-in)")
            wave3.append((leverage, float(freed), mid, a))
            surgery_modules.add(mid)
        elif reco and reco.get("dividable") and leverage >= surgery_tail:
            a = _action(
                f"mod:{mid}", "split_module", 3, mid,
                f"Split {label} into ~{reco['n_subunits']} sub-modules",
                f"Editing it recompiles {reco['dependents']} module(s) "
                f"(~{reco['downstream_human']} downstream)"
                + ("; it sits on the cold critical path" if reco["crit"]
                   else "")
                + f". {reco['split_public']} type(s) would go public.",
                payoff=leverage, effort=reco["split_public"], roi=None,
                module=mid)
            if iso:
                top = iso["candidates"][0]
                a["details"]["alternative"] = (
                    f"or isolate {top['type']} "
                    f"({iso['summary']['top_share']}% of external fan-in)")
            wave3.append((leverage, leverage, mid, a))
            surgery_modules.add(mid)
        elif iso:
            _iso_or_defer(mid, iso, leverage,
                          float(iso["candidates"][0]["roi"]))
        elif reco and leverage >= surgery_tail:
            part = partitions.get(mid)
            if part and part["verdict"] == "no_seam" and part["blockers"]:
                hubs = ", ".join(b["type"] for b in part["blockers"][:3])
                deferred.append(_deferred(
                    f"mod:{mid}", "partition_module", mid,
                    f"Partition {label}", "no_seam",
                    f"Editing it recompiles {reco['dependents']} module(s), "
                    f"but {part['summary']['reason']} — no usage seam yet. "
                    f"Blocking hub(s): {hubs}. Isolate or invert them first "
                    f"to create the seam."))
            else:
                deferred.append(_deferred(
                    f"mod:{mid}", "stabilize_api", mid,
                    f"Narrow {label}'s public API", "stabilize_api",
                    f"Editing it recompiles {reco['dependents']} module(s) "
                    f"but there is no sub-folder seam to split on — shrink "
                    f"and freeze its public interface instead."))
        else:
            ignored_tail += 1

    # API retrofit: an existing SPM module with several direct consumers and
    # no <Name>API counterpart. The split decision the master plan makes for
    # *new* extractions, applied retroactively — otherwise the equilibrium's
    # api_coverage criterion can fail with an empty feed and nothing to do.
    all_labels = {n.get("label", n["id"]) for n in nodes}
    for mid in sorted(spm_nodes):
        if mid in surgery_modules:
            continue
        n = spm_nodes[mid]
        label = n.get("label", mid)
        if label.endswith("API") or f"{label}API" in all_labels:
            continue
        consumers = sorted(direct_deps.get(mid, ()))
        if len(consumers) < API_MIN_CONSUMERS:
            continue
        leverage = 0.0
        reco = reco_by_id.get(mid)
        if reco:
            leverage = (reco["hot"] if reco.get("hot") is not None
                        else reco.get("combined", 0))
        a = _action(
            f"api:{mid}", "api_retrofit", 3, mid,
            f"Give {label} an API package ({label}API)",
            f"{len(consumers)} module(s) import {label} directly — every "
            f"implementation edit cascades into all of them. A {label}API "
            f"package (protocols + value types) firewalls the warm rebuild: "
            f"consumers rewire to the API, only the composition root keeps "
            f"the implementation.",
            payoff=float(n.get("warm", 0)), effort=float(n.get("types", 0)),
            roi=None, module=mid, consumers=len(consumers))
        wave3.append((leverage, float(n.get("warm", 0)), mid, a))
        surgery_modules.add(mid)

    wave3.sort(key=lambda t: (-t[0], -t[1], t[2]))
    # Record the leverage each wave-3 action was ranked by: the master plan
    # re-sorts these against the *post-extraction* graph (the baseline is stale
    # once waves 1-2 move folders) and falls back to this value for any subject
    # absent from the simulated module set.
    for lev, _n, _m, a in wave3:
        a["details"]["leverage"] = round(float(lev), 1)
    actions.extend(a for _l, _n, _m, a in wave3)

    # ---- wave 4: joins — boundary without benefit ----------------------------
    # A module about to receive an absorbed folder keeps its boundary — never
    # suggest folding it away in the same plan.
    absorb_dests = {
        (qw.get("destination") or {}).get("module")
        for qw in qw_items if qw.get("destination")
    }
    for mid in sorted(spm_nodes):
        if mid in surgery_modules or mid in absorb_dests:
            continue
        n = spm_nodes[mid]
        # An API package exists to be small and single-purpose; one consumer
        # today (often just its impl) is the convention working, not a
        # boundary without benefit. Never advise folding it.
        if n.get("label", mid).endswith("API"):
            continue
        deps = direct_deps.get(mid, set())
        types = n.get("types") or 0
        if not deps:
            deferred.append(_deferred(
                f"join:{mid}", "join_module", mid,
                f"Remove or fold {n.get('label', mid)}", "unused",
                "No first-party type references reach it — verify "
                "(resources, @main, dynamic lookup) and consider removing "
                "or folding it."))
            continue
        if len(deps) != 1 or types > ADVISOR_JOIN_MAX_TYPES:
            continue
        consumer = next(iter(deps))
        if consumer not in spm_nodes:
            continue
        c_label = spm_nodes[consumer].get("label", consumer)
        actions.append(_action(
            f"join:{mid}", "join_module", 4, mid,
            f"Fold {n.get('label', mid)} into {c_label}",
            f"Only {c_label} depends on it and it holds {types} type(s) — "
            f"a module boundary without isolation benefit; folding it "
            f"removes a manifest and a resolution step.",
            payoff=0.0, effort=types, roi=None,
            module=mid, into=consumer))

    kinds: dict[str, int] = defaultdict(int)
    for a in actions:
        kinds[a["kind"]] += 1
    waves: dict[str, int] = defaultdict(int)
    for a in actions:
        waves[str(a["wave"])] += 1
    summary = {
        "actions": len(actions),
        "deferred": len(deferred),
        "kinds": dict(sorted(kinds.items())),
        "waves": dict(sorted(waves.items())),
        "ignored_tail_modules": ignored_tail,
        "stop": (f"After these {len(actions)} action(s), every remaining "
                 f"option costs more than it pays — stop, re-render, and let "
                 f"the board reshuffle." if actions else
                 "Nothing pays right now — the graph is as modular as the "
                 "evidence supports."),
    }
    return {"actions": actions, "deferred": deferred, "summary": summary}
