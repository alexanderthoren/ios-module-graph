"""Flatten the migration plan into PR-sized tasks; markdown / json writers."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def build_task_list(
    plan: list[dict],
    file_records: list[dict],
    root_label: str,
    root_path: str,
    migrated_prefixes: list[str],
    initial_migrated_count: int,
    source_total: int,
) -> list[dict]:
    """Flatten the plan into a sequence of PR-sized refactor tasks.

    For singleton steps: one task = extract that folder.
    For cycle steps: one task per extraction candidate inside the cycle, ordered
    lightest first. Each task carries enough info for an automated agent to
    execute it: target folder, files involved, outgoing refs to refactor, and
    the specific type names that need to be abstracted/moved.
    """
    files_by_folder: dict[str, list[str]] = defaultdict(list)
    for f in file_records:
        files_by_folder[f["folder"]].append(f["name"])

    tasks: list[dict] = []
    task_num = 0

    for step in plan:
        if not step["is_cycle"]:
            folder = step["folders"][0]
            task_num += 1
            tasks.append({
                "id": task_num,
                "plan_step": step["step"],
                "type": "extract_leaf",
                "folder": folder,
                "files": sorted(files_by_folder.get(folder, [])),
                "files_count": len(files_by_folder.get(folder, [])),
                "refactor_edges": [],
                "outgoing_refs_to_refactor": 0,
                "unlocks": [
                    {"folder": u["folders"][0] if u["size"] == 1 else None,
                     "size": u["size"]}
                    for u in step["unlocks"]
                ],
                "notes": (
                    "This folder has zero first-party outgoing deps. "
                    "Extract directly: create a new SPM target, move the files, "
                    "update consumers to import the new module."
                ),
            })
        else:
            cycle_size = step["size"]
            for ext in step["extractions"]:
                task_num += 1
                folder = ext["folder"]
                tasks.append({
                    "id": task_num,
                    "plan_step": step["step"],
                    "type": "extract_from_cycle",
                    "folder": folder,
                    "files": sorted(files_by_folder.get(folder, [])),
                    "files_count": len(files_by_folder.get(folder, [])),
                    "refactor_edges": [
                        {
                            "target_folder": e["dst"],
                            "refs": e["refs"],
                            "types": e["types"],
                        }
                        for e in ext["edges"]
                    ],
                    "outgoing_refs_to_refactor": ext["outgoing_refs"],
                    "incoming_consumers": ext["incoming_count"],
                    "cycle_size": cycle_size,
                    "unlocks": [],
                    "notes": (
                        f"Member of a {cycle_size}-folder cycle. "
                        "Before extraction, refactor every outgoing reference: "
                        "move the type into a shared module, invert the dependency via "
                        "a protocol the target folder owns, or move this folder's "
                        "responsibility into the target. Then move the folder into "
                        "its own SPM target."
                    ),
                })

    return tasks


def write_task_list_markdown(tasks: list[dict], meta: dict, out_path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# Migration Plan — {meta['root_label']}")
    lines.append("")
    lines.append(f"Project root: `{meta['root_path']}`  ")
    lines.append(f"Folders already in SPM (baseline): **{meta['baseline_count']}**"
                 f" — prefixes: `{', '.join(meta['migrated_prefixes']) or '(none)'}`  ")
    excluded_count = meta.get('excluded_count', 0)
    blocked_reasons = meta.get('blocked_reasons', {}) or {}
    blocked_count = len(blocked_reasons)
    if excluded_count:
        lines.append(f"Folders marked won't-be-modularized (excluded): **{excluded_count}**  ")
    if blocked_count:
        lines.append(f"Folders that can't be modularized (depend on excluded): **{blocked_count}**  ")
    lines.append(f"Folders to extract: "
                 f"**{meta['source_total'] - meta['baseline_count'] - excluded_count - blocked_count}**  ")
    lines.append(f"Total tasks: **{len(tasks)}**")
    lines.append("")
    lines.append("## How to use this list")
    lines.append("")
    lines.append(
        "Each task is one PR. Tasks are ordered: complete them sequentially. "
        "Within a single task, do **not** combine refactor + move + import update "
        "for multiple folders — one folder, one PR. After each task ships, "
        "regenerate this plan (`python3 -m modgraph <root> --list`) so the "
        "remaining plan reflects the new state."
    )
    lines.append("")
    lines.append("Task types:")
    lines.append("- `extract_leaf` — folder has zero remaining first-party deps. "
                 "Just move it.")
    lines.append("- `extract_from_cycle` — folder participates in a dependency cycle. "
                 "Refactor each outgoing edge first, then move.")
    lines.append("")
    lines.append("---")
    lines.append("")
    for t in tasks:
        lines.append(f"## Task {t['id']} — `{t['folder']}`")
        lines.append("")
        lines.append(f"- **Type:** `{t['type']}`")
        lines.append(f"- **Plan step:** {t['plan_step']}")
        lines.append(f"- **Files:** {t['files_count']}")
        if t["files"]:
            lines.append("  - " + ", ".join(f"`{n}`" for n in t["files"]))
        if t["type"] == "extract_from_cycle":
            lines.append(f"- **Cycle size:** {t['cycle_size']} folders")
            lines.append(f"- **Outgoing refs to refactor:** {t['outgoing_refs_to_refactor']}")
            lines.append(f"- **Incoming consumers:** {t['incoming_consumers']}")
        if t["refactor_edges"]:
            lines.append("- **Edges to refactor:**")
            for e in t["refactor_edges"]:
                types_str = ", ".join(f"`{x}`" for x in e["types"]) or "(no specific types resolved)"
                lines.append(
                    f"  - → `{e['target_folder']}` ({e['refs']} ref"
                    f"{'' if e['refs'] == 1 else 's'}): {types_str}"
                )
        if t["unlocks"]:
            unlocked_strs = [
                f"`{u['folder']}`" if u["folder"]
                else f"⚠cycle of {u['size']}"
                for u in t["unlocks"]
            ]
            lines.append("- **Unlocks:** " + ", ".join(unlocked_strs))
        lines.append("")
        lines.append("**Instructions for this PR:**")
        lines.append("")
        lines.append(t["notes"])
        if t["type"] == "extract_from_cycle" and t["refactor_edges"]:
            lines.append("")
            lines.append("Steps:")
            lines.append("")
            lines.append("1. For each edge listed above, refactor so the source folder "
                         "no longer references types in the target folder. Options:")
            lines.append("   - Move the referenced type into a shared lower-level module "
                         "both folders can depend on.")
            lines.append("   - Define a protocol in this folder that the target folder "
                         "conforms to (dependency inversion).")
            lines.append("   - Pass the dependency in by injection from a coordinator above.")
            lines.append("2. Verify the codebase still compiles and tests pass.")
            lines.append("3. Re-run the analysis to confirm this folder's outgoing refs "
                         "are now zero (or only go to already-migrated folders).")
            lines.append("4. Create a new SPM target/package, move the folder's files into it.")
            lines.append("5. Update consumers to import the new module.")
        else:
            lines.append("")
            lines.append("Steps:")
            lines.append("")
            lines.append("1. Create a new SPM target/package for this folder.")
            lines.append("2. Move every file into the new module's sources.")
            lines.append("3. Mark types `public` where consumers need them.")
            lines.append("4. Update every consumer to import the new module.")
            lines.append("5. Verify the build and tests are green.")
        lines.append("")
        lines.append("---")
        lines.append("")

    if blocked_count:
        lines.append("## Can't modularize (blocked by won't-modularize)")
        lines.append("")
        lines.append(
            "These folders are out of the migration plan because they "
            "transitively depend on a folder marked won't-modularize. An SPM "
            "package cannot link against non-SPM app-target code, so they "
            "stay where they are until the blocking dependency is severed "
            "(or the blocker is unmarked)."
        )
        lines.append("")
        for folder in sorted(blocked_reasons):
            blockers = blocked_reasons[folder]
            blockers_str = ", ".join(f"`{b}`" for b in blockers)
            lines.append(f"- `{folder}` — blocked by: {blockers_str}")
        lines.append("")
        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_task_list_json(tasks: list[dict], meta: dict, out_path: Path) -> None:
    out_path.write_text(
        json.dumps({"meta": meta, "tasks": tasks}, indent=2),
        encoding="utf-8",
    )
