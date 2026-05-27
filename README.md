# ios-module-graph

Accurate, **folder-level Swift dependency graph + SPM-migration planner**, built
from the compiler's own index store.

Point it at any iOS/Swift project. It builds the project once to populate the
compiler index store, resolves every type reference **by USR** (so name
collisions never fabricate edges), and emits:

- an **interactive HTML graph** you can drill into folder-by-folder, and
- a **migration task list** (markdown or JSON) — a topologically-ordered,
  PR-sized plan for extracting folders into SPM packages.

Folders already migrated to SPM (any `Package.swift` subtree, found
recursively) are detected automatically and treated as completed — edges into
them don't block the plan.

---

## Why the index store

A regex scanner can't tell which `Foo` a reference binds to when several
folders declare a type named `Foo`, so it invents phantom edges. This tool
reads the index store Apple's compiler writes during a build and resolves each
reference to the **exact** declaration by USR. Edges are real.

---

## Requirements

| Tool | Why | Notes |
|------|-----|-------|
| [`just`](https://github.com/casey/just) | task runner | `brew install just` |
| Swift 6.x toolchain | builds the index-store reader (`swift build`) | ships with Xcode |
| Python 3 | renders the HTML / task list | stdlib only, no pip deps |
| Xcode + `xcodebuild` | builds the target project (xcode mode) | only for `.xcworkspace`/`.xcodeproj` projects |
| `xcsift` | formats `xcodebuild` output | only used in xcode mode |

The reader depends on `apple/indexstore-db` (`release/6.3`), fetched once on
first build. If it fails to resolve, match the branch to your toolchain in
`index_graph/Package.swift`.

---

## Setup

Create a gitignored `.env` in this directory pointing at the project to analyse:

```sh
PROJECT_DIR=$HOME/path/to/MyApp
WORKSPACE=MyApp.xcworkspace   # optional — auto-detected if unset
SCHEME=MyApp                  # optional — auto-detected if unset
```

`WORKSPACE`/`SCHEME` can be left out: the tool auto-detects a single
`.xcworkspace`/`.xcodeproj` and a scheme of the same basename. CLI vars
override `.env`; `.env` overrides the built-in defaults.

---

## Usage

```sh
just tree     # interactive HTML dependency graph  → dependency_graph.html
just list     # migration task list (markdown)      → migration_plan.md
just all      # both
just clean    # wipe generated files (forces a full rebuild next run)
```

`tree`/`list`/`all` reuse the resolved graph (`index_graph.json`) from the last
run, so re-rendering is instant. The graph is rebuilt automatically when it's
missing — the first run, or after `just clean`. To force a rebuild from zero:

```sh
just clean && just tree
```

### Per-run overrides

Everything in `.env` is also a `just` CLI var (CLI wins):

```sh
just tree project_dir=/other/App workspace=Other.xcworkspace scheme=Other
```

---

## Build modes

`BUILD_MODE` (env or CLI var) selects how the project is built to populate the
index store:

| Mode | When | Build command |
|------|------|---------------|
| `auto` (default) | — | `.xcworkspace`/`.xcodeproj` present ⇒ `xcode`; else `Package.swift` ⇒ `spm` |
| `xcode` | Xcode-managed project | `xcodebuild clean build -workspace/-project -scheme` |
| `spm` | pure SwiftPM package, no Xcode project | `swift build` |

The build runs arm64-only against an iOS simulator destination. A non-zero
build exit is tolerated **only** if the index store still populated (indexing
finishes before linking, so link errors don't block analysis); a real compile
failure leaves the store empty and hard-fails.

Escape hatches for builds that need extra flags (e.g. an iOS SDK/target for a
pure-SPM build of UIKit code):

```sh
SWIFT_BUILD_FLAGS="-Xswiftc -sdk -Xswiftc $(xcrun --sdk iphonesimulator --show-sdk-path)"
XCODE_BUILD_FLAGS="-quiet"
CONFIG=Release
DEST="generic/platform=iOS Simulator"
```

---

## How it works

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
   │ 3. find_leaf_modules.py  builds the folder graph, computes │
   │    an SCC-aware topological migration order →              │
   │    dependency_graph.html / migration_plan.md               │
   └───────────────────────────────────────────────────────────┘
```

The migration plan is **SCC-aware**: folders that are cyclically coupled are
bundled into a single step (you can't extract them independently). It shows
"start here → next → next" guidance and which prerequisites must move first.
