<div align="center">

# 📦 ios-module-graph

**An accurate, folder-level Swift dependency graph + SPM-migration planner — built from the compiler's own index store.**

[![Swift 6](https://img.shields.io/badge/Swift-6.x-F05138?logo=swift&logoColor=white)](https://swift.org)
[![Python 3](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![just](https://img.shields.io/badge/built%20with-just-1abc9c)](https://github.com/casey/just)
[![platform](https://img.shields.io/badge/platform-macOS%20%7C%20iOS-lightgrey)](#requirements)

<img src="docs/explore.png" alt="Explore mode — interactive folder-level dependency graph" width="100%">
<img src="docs/migrate.png" alt="Migration mode — SPM migration planner with progress tracking" width="100%">

</div>

---

Point it at **any** iOS/Swift project. It builds the project once to populate the
compiler index store, resolves every type reference **by USR**, and gives you:

- 🕸️ **An interactive HTML graph** — drill into folders, then into individual **types**, and see real reference edges.
- 🗺️ **A migration plan** — a topologically-ordered, PR-sized path for extracting folders into SPM packages.
- ✅ **Auto-detected progress** — folders already in SPM (any `Package.swift` subtree, found recursively) are marked done and stop blocking the plan.
- 🔍 **Two modes** — *Explore* to understand the codebase, *Migration* to plan and execute the SPM split (with a guided **setup wizard**).
- 🌳 **Call-tree popups & type-level view** — open any folder/file/type in a focused reference tree, or drill a folder down to which of its types are extractable now vs. blocked.
- 🎯 **Preview & retarget** — preview a migration step in the graph and reassign its folders to new or existing SPM packages/targets before you commit.
- 🤖 **Ready-to-paste Claude prompts** — generate a migration prompt for the planned moves (code + tests), or an investigation prompt that asks Claude to recommend the destination package/module.
- ⚡ **Live mode** — serve the graph on localhost, hot-reload it on every rebuild, and `cmd`+click a node to jump straight into Xcode.

> **Edges are real.** A regex scanner can't tell which `Foo` a reference binds to
> when several folders declare a type named `Foo`, so it fabricates edges. This
> tool reads the index store Apple's compiler writes during a build and resolves
> every reference to the **exact** declaration by USR — no phantom edges.

---

## 🚀 Quick start

```sh
# 1. install the task runner
brew install just

# 2. point it at your project (gitignored — never committed)
cat > .env <<'ENV'
PROJECT_DIR=$HOME/path/to/MyApp
WORKSPACE=MyApp.xcworkspace   # optional — auto-detected if unset
SCHEME=MyApp                  # optional — auto-detected if unset
ENV

# 3. build the graph
just tree
```

The first run builds your project, indexes it, and opens
`dependency_graph.html`. Subsequent runs are instant.

---

## 🛠️ Commands

| Command | Output |
|---------|--------|
| `just tree` | interactive HTML graph → `dependency_graph.html` |
| `just list` | migration task list → `migration_plan.md` |
| `just all` | both |
| `just serve` | live mode — serve the HTML, hot-reload on rebuild, `cmd`+click → Xcode |
| `just test` | run the Python test suite (stdlib unittest) |
| `just clean` | wipe generated files (forces a full rebuild next run) |

`tree`/`list`/`all` reuse the resolved graph (`index_graph.json`) from the last
run, so re-rendering is instant. It's rebuilt automatically when missing — the
first run, or after `just clean`. **To force a rebuild from zero:**

```sh
just clean && just tree
```

**Per-run overrides** — every `.env` key is also a `just` CLI var (CLI wins):

```sh
just tree project_dir=/other/App workspace=Other.xcworkspace scheme=Other
```

---

## 🧭 Modes & tools

A toggle at the top switches the whole UI between two modes:

- **🔍 Explore** — understand the codebase. Every folder is neutral-colored; migration state is hidden so it doesn't get in the way. Already-SPM folders stay in scope so you can see SPM-to-SPM coupling.
- **🧭 Migration** — plan and execute the split. Adds a **Setup** wizard, a **Plan** tab, and per-node migration state (leaf / blocked / migrated / won't-modularize).

Tools available while exploring or planning:

- **Type-level drill-down** — click into a folder to see its individual types as a graph. Each type is colored by whether it's **extractable now** (no external deps), **moves with the folder** (only intra-folder refs), or **blocked** (depends on other folders).
- **🌳 Call tree** — open any folder, file, or type in a focused popup that renders its reference tree in its own graph, so you can trace what pulls in what without losing your place.
- **🔍 Preview & retarget** — from a plan step, preview the affected folders in the graph and reassign them to a **new or existing SPM package/target** before committing to the move.
- **🤖 Prompt generators** — once a step is scoped, generate a ready-to-paste **Claude prompt**:
  - *migration prompt* — describes every move in the step (including relocating tests into the new module's test target).
  - *investigation prompt* — when you don't yet know the destination, asks Claude to inspect the repo (with dependency context attached) and recommend the package, module name, and approach first.
- **✂️ Divide into modules** — hover any folder big enough to split and hit **Divide**. It treats the folder's immediate subfolders as candidate sub-modules and shows a **public-API cost** table (how many types each sub-module would expose as `public`) and an **SCC-aware extraction order**. Each step has a **📊 Visualize** before/after graph and a **📋 Copy prompt** button that builds a ready-to-paste Claude prompt for that step. See *Divide a module* below.

---

## ✂️ Divide a module

The migration plan extracts whole folders of the app into SPM. **Divide** does the
inverse-scale job: you already have a module that grew too big — how do you split
*it* into smaller modules? It lives entirely in the HTML graph — hover a folder
node and hit **Divide**.

Candidate sub-modules are the module's **immediate subfolders**. For each it
computes the **public-API surface** — the types referenced from another
sub-module, which must flip `internal`→`public` once the boundary is a module
boundary. A near-zero percentage is a clean extraction; a high one means the
folder is glue that leaks most of its types and splitting it buys little. The
extraction order bundles cyclically-coupled sub-modules into one step and lists
the feedback-arc-set edges to sever before they can become separate modules.

Per extraction step you get:

- **📊 Visualize** — a before/after graph of the sub-module units. *Before* shows
  the current coupling with the edges this step must cut in red; *after* shows the
  step's units extracted (green), those edges removed, and surviving references
  redrawn as cross-module imports (blue).
- **📋 Copy prompt** — a ready-to-paste Claude prompt for that step. Leaf steps get
  an **extraction** prompt (which files to move, which types become `public`, which
  consumers to update). Cycle bundles get a **refactor** prompt (the exact
  references to sever to break the cycle — a cyclic bundle can't be moved as-is).

Requires the index path (the public-API cost needs the USR-resolved references),
so the Divide action only appears when the graph was built from the index store.

---

## ⚡ Live mode

```sh
just serve            # serves dependency_graph.html on http://localhost:8765
```

`just serve` hosts the graph locally and **hot-reloads** it whenever `just tree`
regenerates the HTML — so in another terminal you can edit code, re-run
`just tree`, and watch the graph update. `cmd`/`ctrl`+click a folder, file, or
type to **open it in Xcode** (via `xed`). Stays running until `Ctrl-C`; override
the port with `just serve port=9000`.

---

## ⚙️ Build modes

`BUILD_MODE` (env or CLI var) selects how the project is built to populate the
index store:

| Mode | When | Build command |
|------|------|---------------|
| `auto` *(default)* | — | `.xcworkspace`/`.xcodeproj` present ⇒ `xcode`; else `Package.swift` ⇒ `spm` |
| `xcode` | Xcode-managed project | `xcodebuild clean build -workspace/-project -scheme` |
| `spm` | pure SwiftPM package | `swift build` |

The build runs arm64-only against an iOS simulator destination. A non-zero build
exit is **tolerated** as long as the index store populated — indexing finishes
before linking, so link errors don't block analysis. A real compile failure
leaves the store empty and hard-fails.

Need extra build flags? (e.g. an iOS SDK/target for a pure-SPM build of UIKit code)

```sh
SWIFT_BUILD_FLAGS="-Xswiftc -sdk -Xswiftc $(xcrun --sdk iphonesimulator --show-sdk-path)"
XCODE_BUILD_FLAGS="-quiet"
CONFIG=Release
DEST="generic/platform=iOS Simulator"
```

---

## 📋 Requirements

| Tool | Why | Notes |
|------|-----|-------|
| [`just`](https://github.com/casey/just) | task runner | `brew install just` |
| Swift 6.x toolchain | builds the index-store reader | ships with Xcode |
| Python 3 | renders the HTML / task list | stdlib only, no pip deps |
| Xcode + `xcodebuild` | builds the target project (xcode mode) | only for `.xcworkspace`/`.xcodeproj` |
| `xcsift` | formats `xcodebuild` output | only used in xcode mode |

The reader depends on `apple/indexstore-db` (`release/6.3`), fetched once on
first build. If it fails to resolve, match the branch to your toolchain in
`index_graph/Package.swift`.

---

## 🧩 How it works

```
                     just tree
                        │
   ┌────────────────────┼─────────────────────────────────────┐
   │ 1. build target project (xcodebuild | swift build)        │
   │    → populates the compiler index store                   │
   ├───────────────────────────────────────────────────────────┤
   │ 2. index_graph (Swift)  reads the store, resolves every    │
   │    reference by USR → index_graph.json                     │
   ├───────────────────────────────────────────────────────────┤
   │ 3. modgraph (Python)  builds the folder graph, computes    │
   │    an SCC-aware topological migration order →              │
   │    dependency_graph.html / migration_plan.md               │
   └───────────────────────────────────────────────────────────┘
```

The migration plan is **SCC-aware**: folders that are cyclically coupled are
bundled into a single step (you can't extract them independently). It shows
"start here → next → next" guidance and which prerequisites must move first. The
plan, graph, and package map are **deterministic** — identical inputs always
produce byte-identical outputs, so regenerating after a change yields a clean diff.

---

## 🗂️ Project layout

```
index_graph/            Stage 1 — Swift index-store reader (SwiftPM)
  Sources/index_graph/main.swift   resolves refs by USR → index_graph.json
modgraph/               Stage 2 — Python package (stdlib only)
  config.py             constants, regexes, default paths
  models.py             GraphData — the typed graph container
  scanner.py            regex-scan fallback (no index store)
  index_loader.py       load the USR-resolved index_graph.json
  graph.py              Tarjan SCC, SCC-aware migration plan, folder tree
  cycles.py             feedback-arc-set + per-folder extraction targets
  spm.py                SPM package map + migrated-prefix auto-detection
  exclusions.py         "won't modularize" set + transitive blocked-by
  tasks.py              flatten the plan into PR-sized tasks; md / json writers
  render.py             inject the payload into template.html
  template.html         the interactive HTML+JS UI
  cli.py                argument parsing + main orchestration
find_leaf_modules.py    thin entry-point shim → modgraph.cli:main
serve.py                companion HTTP server for `just serve` (live mode)
justfile                the glue: build → index → resolve → render
tests/                  stdlib-unittest suite for the modgraph package
```

---

## 🧑‍💻 Direct CLI

`just` is the easy path, but the Python stage is a normal CLI you can drive
directly (this is what the recipes call). Equivalent: `python3 -m modgraph …`.

```sh
python3 find_leaf_modules.py <project_root> [options]
```

| Option | Effect |
|--------|--------|
| `--from-index JSON` | load the USR-resolved graph from `index_graph.json` (the accurate path) instead of regex-scanning |
| `--graph [PATH]` | write the interactive HTML graph (default `dependency_graph.html`; implied if neither `--graph` nor `--list` is given) |
| `--list [PATH]` | write the migration task list (default `migration_plan.md`) |
| `--list-format markdown\|json` | task-list format (default `markdown`) |
| `--migrated-prefix PREFIX` | mark a path prefix as already-in-SPM (repeatable; auto-detected from `Package.swift` subtrees) |
| `--no-auto-detect-spm` | disable `Package.swift` auto-detection |
| `--excluded-file JSON` | folders flagged *won't be modularized* (the graph's Exclude button writes this) |
| `--include-tests` | include `Tests`/`UITests`/`SnapshotTests` folders (skipped by default) |
| `--ignore PATTERN` | glob to skip, matched against dir name **or** relative path (repeatable) |
| `--label NAME` | display label for the root (default: directory basename) |
| `--ext .swift` | file extension to scan (only Swift is fully supported) |

Without `--from-index` the tool falls back to a pure regex scan — fast, but it
fabricates edges when two folders declare a same-named type. Prefer the index
path; that's the reason stage 1 exists.

---

## 🧪 Development

The Python package has a stdlib-`unittest` suite — **no pip dependencies**:

```sh
just test                                    # whole suite (alias: just tests)
python3 -m unittest discover -s tests -v     # whole suite (from repo root)
python3 -m unittest tests.test_graph -v      # a single module
```

`tests/fixtures.py` defines one shared toy project the tests assert against;
`tests/test_graph.py` is the style exemplar. There is no linter — verification is
a green test run plus a clean `just tree` regeneration.
