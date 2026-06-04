# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A folder-level Swift dependency-graph + SPM-migration planner. It builds a
target iOS/Swift project once to populate the **compiler index store**, resolves
every type reference by USR, and renders an interactive HTML graph + a
topologically-ordered migration task list. It is generic — pointed at any
project via `.env`/CLI vars, with no hardcoded project identity.

See `README.md` for user-facing usage. This file covers the internals.

## Commands

`just` is the only entry point for the full pipeline. Recipes:

```sh
just tree     # HTML graph        → dependency_graph.html
just list     # migration plan    → migration_plan.md
just all      # both
just serve    # live mode: serve HTML, hot-reload on rebuild, cmd+click → Xcode
just test     # run the Python test suite (stdlib unittest; alias: just tests)
just clean    # wipe generated files; next run rebuilds from scratch
```

`tree`/`list`/`all` reuse `index_graph.json` if present and rebuild it (full
project build → index → resolve) only when missing. There is **no `--clean`
flag** — force a rebuild with `just clean` then re-run.

Build the Swift reader alone:

```sh
cd index_graph && swift build -c release
```

### Tests

The Python stage has a stdlib-`unittest` suite (no pytest, no pip deps):

```sh
just test                                     # whole suite (any CWD)
python3 -m unittest discover -s tests -v      # whole suite (from repo root)
python3 -m unittest tests.test_graph -v       # one module
```

`just test` (alias `just tests`) drops the cli tests' stdout noise so only the
unittest report shows, and exits non-zero if anything fails.

`tests/fixtures.py` holds one shared toy project (4 folders, one 2-folder cycle,
one multi-declared type) reused across the suite — assert against it.
`tests/test_graph.py` is the **exemplar**: match its style (one behaviour per
test, concrete assertions, `tempfile.TemporaryDirectory` for any file IO, never
write to the repo root or under `modgraph/`).

There is no linter. Full verification = run the suite **and** regenerate the
outputs (`just tree` / re-run against a cached `index_graph.json`) without error.

## Architecture: a two-stage pipeline

```
target project ──(xcodebuild | swift build)──▶ compiler index store
                                                       │
                  index_graph (Swift) reads store, resolves refs by USR
                                                       │
                                              index_graph.json
                                                       │
              modgraph (Python) builds folder graph, computes plan
                                                       │
                              dependency_graph.html / migration_plan.md
```

**Stage 1 — `index_graph/` (Swift, SwiftPM).** Reads the index store via Apple's
`indexstore-db`, keeps only first-party declarations (under repoRoot, excluding
build/checkout fragments), and emits the graph as JSON. Invoked as
`index_graph <storePath> <repoRoot> [outJSON]`. The `Graph` Codable struct in
`Sources/index_graph/main.swift` **is the data contract** — its shape must stay
in sync with `load_index_graph` in `modgraph/index_loader.py`. Change one, change
the other.

**Stage 2 — `modgraph/` (Python package, stdlib only).** Loads the resolved
graph (`--from-index`), builds the folder-level dependency graph, computes an
**SCC-aware** topological migration order (cyclically-coupled folders bundle into
one step), and renders both outputs. A regex-scanner fallback exists (no
`--from-index`) but produces phantom edges on name collisions — the whole point
of stage 1 is to avoid it; prefer the index path.

The package is invoked as `python3 -m modgraph …` (via `modgraph/__main__.py` →
`modgraph.cli:main`); the justfile recipes call exactly that. There is no
root-level entry script — keep the repo root free of loose Python.

**The justfile is the glue.** It detects build mode, builds the project to
populate the store, builds the reader, runs the reader, then runs the renderer.

### `modgraph/` module map

| module | responsibility |
|--------|----------------|
| `config.py` | constants, regexes, default output paths (relative to repo root via `REPO_ROOT`) |
| `models.py` | `GraphData` dataclass — the typed container both producers return |
| `scanner.py` | regex-scan fallback path (`scan`, `strip_noise`, `should_skip_dir`, `collect_swift_files`, `compute_pair_types`) |
| `index_loader.py` | `load_index_graph` — parse the USR-resolved `index_graph.json` |
| `graph.py` | `_tarjan_sccs`, `compute_migration_plan` (SCC-aware, deterministic), `build_tree` |
| `cycles.py` | `_feedback_arc_set`, `compute_cycle_breakers`, `compute_extraction_targets` |
| `spm.py` | `_build_package_map`, `auto_detect_migrated_prefixes`, `is_migrated`, `_package_label` |
| `divide.py` | split ONE module into smaller modules: `aggregate_module` (subfolders→units), `public_surface`, `compute_division_plan` (reuses `graph`/`cycles`), `dividable_modules` |
| `build_impact.py` | `compute_build_impact` — generic **warm-rebuild blast radius** (transitive reverse-dependents) + **cold-build cohort/critical path** (SCC-condensed, reuses `_tarjan_sccs`) scorer over any node/edge set; consumed by `module_graph` |
| `module_graph.py` | `module_of`, `compute_module_graph` — collapse the folder graph to real **compile units** (SPM targets + one app target), score via `build_impact`; powers "Build" mode |
| `build_recommendations.py` | `compute_split_recommendations` — rank modules by the build-time **payoff of separating** them (warm cascade + cold critical-path contribution), link dividable ones to `divisions`; powers Build mode's "Split plan" tab |
| `build_times.py` | `aggregate_stats_dir`, `load_build_times` (+ CLI) — sum real per-module compile times from the Swift compiler's `-stats-output-dir` output captured during the cold build; feeds Build mode's module cost |
| `exclusions.py` | `load_exclusions`, `compute_blocked_by_excluded` |
| `tasks.py` | `build_task_list`, `write_task_list_markdown`, `write_task_list_json` |
| `render.py` | `render_html` — inject the JSON payload into `templates/template.html` |
| `cli.py` | `parse_args` + `main` orchestration |
| `__main__.py` | `python3 -m modgraph` entry point → `cli.main` |
| `serve.py` | live-mode HTTP server for `just serve` (`python3 -m modgraph.serve`); SSE hot-reload + `xed` bridge |
| `templates/template.html` | the entire HTML+JS UI (extracted from the old embedded literal) |

Both `scan()` and `load_index_graph()` return a **`GraphData`** (see
`models.py`); `cli.main` unpacks it. The scan path leaves the index-only fields
(`pair_types=None`, `type_kinds`/`file_edges`/`type_edges` empty) at their
defaults. When you add a field to the Swift `Graph` contract, add it to
`GraphData`, set it in `load_index_graph`, and (if the UI needs it) ship it in
`render.py`'s payload.

### Dividing one module (the inverse of whole-project migration)

The migration plan extracts **whole folders** of the app into SPM in dependency
order. The **divide** feature does the *inverse-scale* job: take one already-large
module (any folder) and plan how to split **it** into smaller modules. It is
**app-only** — there is no CLI flag; it lives entirely in the HTML graph (the user
hits "Divide" on a folder node). The units are the module's **immediate
subfolders** (a spike showed unsupervised clustering over-fragments, ignores edge
direction → cyclic/illegal modules, and loses to the folder structure on
public-API cost). `divide.aggregate_module` collapses the folder graph inside the
prefix to subfolder→subfolder edges by mapping each endpoint through `unit_of`,
then reuses `compute_migration_plan` +
`compute_cycle_breakers`/`compute_extraction_targets` unchanged — the only new
metric is **public surface** (`public_surface`): per candidate sub-module, the
count of its types referenced from another unit, which must flip
`internal`→`public`. Needs the index path: `pair_types` is empty on the
regex-scan fallback, so `cli.main` only precomputes divisions when it's present.

`cli.main` precomputes a division for every `dividable_modules` folder and ships
them as `payload["divisions"]` (folder id → plan, including `unit_edges` for the
per-step graph); the UI renders everything client-side (the "Divide" modal, the
per-step before/after preview, and the per-step Claude prompts) — no recompute in
the browser, all graph logic stays in Python. `compute_division_plan`'s output is
deterministic for the same reasons the migration plan is (it reuses that code and
sorts the unit rows / `unit_edges`); keep it that way.

### Build mode (module-level warm/cold build-cost graph)

A third app mode (alongside Explore/Migration) that visualizes **build cost at
real compile-unit granularity**, to guide modularization for faster builds. The
key idea: a *folder* is not a compile unit — only **SPM targets** (`…/Sources/<Target>`)
and the single **xcodeproj app target** are. So Build mode forgets folders:
`module_graph.compute_module_graph(...)` maps every folder to its build unit
(`module_of`), collapses folder edges to module→module edges, and scores each
module via `build_impact.compute_build_impact` (which is node/edge-agnostic). It
ships `payload["module_graph"]` = `{"nodes": [{id,label,kind,folders,types,warm,
warm_pct,fan_in,level,crit,scc}], "edges": [{from,to,w}], "summary": {…}}`.

The UI renders this as a **flat module graph** (no folder drill-in — the Hierarchy
tab is hidden in Build mode; `render()` routes straight to `renderModuleGraph()`).
Two lenses (toggle, repaints in place via `recolorModules`): **warm** = transitive
reverse-dependents (touch a module → this many recompile; a worst-case upper bound
since Swift only cascades on public-interface change); **cold** = SCC-condensed
build cohort (`level`, parallelizable within a level) + `crit` (on a globally-longest
chain). Hovering a node lights up its **rebuild set** (transitive dependents) with
red edges (`buildRebuildClosure` + `applyBuildHoverHighlight`, reverse-BFS over the
displayed edges). Collapsing to modules is also *more correct*: folder-level cycles
vanish (they're intra-module and irrelevant — a module compiles atomically), so the
Fever graph drops from 28 folder cycles to **0 module cycles**.

Deterministic (sorted nodes/edges; reuses `_tarjan_sccs`). **Structural only** — no
git churn weighting yet (the obvious v2: rank by `blast_radius × commit-frequency`
to separate "foundational" from "actually hurts").

Build mode has a second tab, **Split plan** (`payload["recommendations"]`, from
`build_recommendations.compute_split_recommendations`): modules ranked by the
build-time payoff of **separating** them. Each module is scored on a **warm** lever
(`downstream_cost` = Σ compile-cost of everything that recompiles when it changes —
splitting localizes edits) and a **cold** lever (its own compile cost *iff on the
critical path* — splitting a big serial module parallelizes its pieces, shortening
the floor). `combined = 50·warm/maxWarm + 50·cold/maxCold` (0–100) is the sort key.
Compile cost is proxied by declared-type count. Each row carries an action: SPM
targets with a precomputed `divisions` plan get "Split into ~N" (the row's **✂️
Divide** button reuses `openDivide(moduleId)`); flat modules get "Stabilize public
API"; the app target gets "Extract features into SPM" (pointing at Migration mode,
since it's one compile unit topping the critical path). Hovering a row highlights
that module's rebuild set on the live graph (`buildHoverModule`).

**Real compile times (optional).** Module *cost* defaults to a type-count proxy, but
`just _index` captures real per-module compile times for free off the cold build it
already runs: it builds with the Swift compiler's `-stats-output-dir` (via
`OTHER_SWIFT_FLAGS`/`-Xswiftc`), so swift-frontend writes one stats JSON per source
file (each with `time.swift-frontend.*.wall`, module name in the filename) into
`.swiftstats/`. After the build, `python3 -m modgraph.build_times <stats-dir>
build_times.json` sums wall-time per module → `{module: seconds}`.
`compute_module_graph` matches SPM targets by label and folds any non-SPM module
(the app's own sources) into the `app` node, attaching `build_ms`/`measured`. When
present, module node size, the warm `downstream_cost`, and the split-payoff ranking
are all in **measured seconds** (UI labels "measured" vs "estimated"). `just clean`
drops both `build_times.json` and `.swiftstats/`.

Why not the `.xcactivitylog`? On this project xcodebuild delegates to the build
service and its log records no per-target compile steps (and the log isn't finalized
through the `xcsift` pipe) — so **xclogparser** finds nothing, and **xcsift
`--build-info`** only recovers durations from the *legacy* build system (always null
here). `-stats-output-dir` is written by the compiler itself, independent of the log.
Caveats: wall time is summed across a module's files (work, not wall-clock — they
compile in parallel); a target with no Swift sources gets no time (proxy fallback);
a module whose Swift module name differs from its SPM target/label would mis-fold
into `app`.

### Why resolution-by-USR matters

When several folders declare a type named `Foo`, a regex scanner can't tell which
one a reference binds to and invents edges. The index store records the USR the
compiler actually bound, so edges are real. This is the project's reason to
exist — do not "simplify" stage 1 back into pure text scanning.

## Things that will bite you

- **The UI lives in `modgraph/templates/template.html`** (~4.5k lines of HTML+JS). It is
  read at runtime by `render.py` (`_load_template()`) and the graph data is
  injected by string-replacing `__PAYLOAD__` (a JSON blob) and `__ROOT_LABEL__`.
  Editing the UI = editing that file directly now (no more
  JavaScript-inside-a-Python-raw-string). Don't introduce a literal
  `__PAYLOAD__`/`__ROOT_LABEL__` into the JS, and remember `str.replace` is
  literal so the injected JSON must stay valid.

- **Outputs are deterministic — keep them that way.** The plan/tree/package-map
  are emitted in a fixed order independent of `PYTHONHASHSEED`. This relies on
  three things that are easy to undo: the tie-break in `compute_migration_plan`
  uses `min(...)` over the **full** first-folder name (not just its first char);
  `unlocks` and the iteration over `scc_rdeps` are sorted; `build_tree` returns a
  key-sorted dict and `_build_package_map` iterates `sorted(folders)`. If you
  introduce a `max(set, …)`/`for x in some_set` whose result reaches the output,
  sort it. The determinism regression tests live in `tests/test_graph.py`.

- **The build is allowed to fail.** `xcodebuild`/`swift build` may exit non-zero
  (e.g. a link error) and that's tolerated — indexing finishes before linking.
  The real gate is the assertion that `<store>/v5/units` exists. A genuine
  compile failure leaves the store empty and hard-fails there. Don't "fix" the
  tolerated non-zero exit.

- **Build mode is auto-detected** (`BUILD_MODE=auto`): `.xcworkspace`/`.xcodeproj`
  ⇒ `xcode` (xcodebuild, needs `xcsift` on PATH); else `Package.swift` ⇒ `spm`
  (`swift build`). The spm path is prepared for splitting an app into multiple
  packages but is less battle-tested; iOS-only SPM code may need
  `SWIFT_BUILD_FLAGS` for an SDK/target.

- **Already-migrated SPM detection is recursive.** `auto_detect_migrated_prefixes`
  (in `spm.py`) walks the whole tree (not just root's children) for
  `Package.swift`, marking each package's `Sources/` as a completed migration. It
  reuses `should_skip_dir` (from `scanner.py`) to avoid descending into
  checkouts/builds. Keep these two in step.

- **`indexstore-db` is pinned** to `release/6.3` in `index_graph/Package.swift`
  to match the local Swift toolchain. If resolution fails after a toolchain bump,
  update that branch. `libIndexStore.dylib` is located via `xcode-select -p`.

## Config

`.env` (gitignored, auto-loaded via `set dotenv-load`) holds the target project:
`PROJECT_DIR`, optional `WORKSPACE`/`SCHEME` (auto-detected if unset), plus
`BUILD_MODE`/`CONFIG`/`DEST`/`SWIFT_BUILD_FLAGS`/`XCODE_BUILD_FLAGS`. Every key
is also a `just` CLI var; CLI overrides `.env` overrides built-in defaults.

Generated artifacts (`index_graph.json`, `dependency_graph.html`,
`migration_plan.md`, `.build/`, `__pycache__`) are gitignored.
