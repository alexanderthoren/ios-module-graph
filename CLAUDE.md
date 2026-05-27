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

`just` is the only entry point. Recipes:

```sh
just tree     # HTML graph        → dependency_graph.html
just list     # migration plan    → migration_plan.md
just all      # both
just clean    # wipe generated files; next run rebuilds from scratch
```

`tree`/`list`/`all` reuse `index_graph.json` if present and rebuild it (full
project build → index → resolve) only when missing. There is **no `--clean`
flag** — force a rebuild with `just clean` then re-run.

Build the Swift reader alone:

```sh
cd index_graph && swift build -c release
```

There is **no test suite and no linter**. Verification = run `just tree` and
confirm the HTML/plan regenerate without error.

## Architecture: a two-stage pipeline

```
target project ──(xcodebuild | swift build)──▶ compiler index store
                                                       │
                  index_graph (Swift) reads store, resolves refs by USR
                                                       │
                                              index_graph.json
                                                       │
              find_leaf_modules.py builds folder graph, computes plan
                                                       │
                              dependency_graph.html / migration_plan.md
```

**Stage 1 — `index_graph/` (Swift, SwiftPM).** Reads the index store via Apple's
`indexstore-db`, keeps only first-party declarations (under repoRoot, excluding
build/checkout fragments), and emits the graph as JSON. Invoked as
`index_graph <storePath> <repoRoot> [outJSON]`. The `Graph` Codable struct in
`Sources/index_graph/main.swift` **is the data contract** — its shape must stay
in sync with `load_index_graph` in the Python tool. Change one, change the other.

**Stage 2 — `find_leaf_modules.py` (Python, stdlib only).** Loads the resolved
graph (`--from-index`), builds the folder-level dependency graph, computes an
**SCC-aware** topological migration order (cyclically-coupled folders bundle
into one step), and renders both outputs. A regex-scanner fallback exists
(no `--from-index`) but produces phantom edges on name collisions — the whole
point of stage 1 is to avoid it; prefer the index path.

**The justfile is the glue.** It detects build mode, builds the project to
populate the store, builds the reader, runs the reader, then runs the renderer.

### Why resolution-by-USR matters

When several folders declare a type named `Foo`, a regex scanner can't tell
which one a reference binds to and invents edges. The index store records the
USR the compiler actually bound, so edges are real. This is the project's
reason to exist — do not "simplify" stage 1 back into pure text scanning.

## Things that will bite you

- **`find_leaf_modules.py` is one ~2700-line file** with the entire HTML+JS UI
  embedded as a single `HTML_TEMPLATE = r"""..."""` literal (~line 631 onward).
  The graph data is injected by string-replacing `__PAYLOAD__`. Editing the UI
  means editing JavaScript-inside-a-Python-raw-string — mind the quoting, and
  don't introduce `"""` or unescaped `__PAYLOAD__` in the JS.

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
  walks the whole tree (not just root's children) for `Package.swift`, marking
  each package's `Sources/` as a completed migration. It reuses `should_skip_dir`
  to avoid descending into checkouts/builds. Keep these two in step.

- **`indexstore-db` is pinned** to `release/6.3` in `index_graph/Package.swift`
  to match the local Swift toolchain. If resolution fails after a toolchain
  bump, update that branch. `libIndexStore.dylib` is located via `xcode-select -p`.

## Config

`.env` (gitignored, auto-loaded via `set dotenv-load`) holds the target project:
`PROJECT_DIR`, optional `WORKSPACE`/`SCHEME` (auto-detected if unset), plus
`BUILD_MODE`/`CONFIG`/`DEST`/`SWIFT_BUILD_FLAGS`/`XCODE_BUILD_FLAGS`. Every key
is also a `just` CLI var; CLI overrides `.env` overrides built-in defaults.

Generated artifacts (`index_graph.json`, `dependency_graph.html`,
`migration_plan.md`, `.build/`, `__pycache__`) are gitignored.
