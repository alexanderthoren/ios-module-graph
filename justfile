# ios-module-graph — accurate Swift dependency graph from the compiler index store.
#
#   just tree             interactive HTML dependency graph
#   just list             migration task list (markdown)
#   just all              both
#   just test             run the Python test suite (stdlib unittest)
#   just clean            wipe generated files (forces a full rebuild next run)
#
# tree/list/all reuse the index_graph.json Swift produced last time, so editing
# the modgraph package and re-rendering is instant. If it's missing — first run,
# or after `just clean` — they rebuild it from a fresh build of the target
# project automatically. To force a rebuild from zero: `just clean` then run.
#
# ── pointing it at a project ───────────────────────────────────────────────────
# Nothing here is tied to a specific app. Point it at any project two ways:
#
#   1. CLI vars (per-run, no files touched):
#        just tree --clean project_dir=/path/to/MyApp workspace=MyApp.xcworkspace scheme=MyApp
#
#   2. Env vars (persist across runs; export or put in a gitignored .env):
#        export PROJECT_DIR=/path/to/MyApp
#        export WORKSPACE=MyApp.xcworkspace   # optional, auto-detected if unset
#        export SCHEME=MyApp                  # optional, auto-detected if unset
#        just tree --clean
#
# CLI vars win over env vars. Env vars win over the built-in defaults below.

# Auto-load a gitignored .env from this dir so env vars persist without export.
set dotenv-load := true

# ── config ───────────────────────────────────────────────────────────────────
# Root of the project to analyse. Override with PROJECT_DIR or project_dir=.
project_dir := env_var_or_default('PROJECT_DIR', env_var('HOME') / "Developer/ios-project")

# Build mode: auto | xcode | spm.
#   auto  → .xcworkspace/.xcodeproj present ⇒ xcode; else Package.swift ⇒ spm
#   xcode → xcodebuild -workspace/-project -scheme (Xcode-managed project)
#   spm   → swift build (pure SwiftPM package, no Xcode project)
build_mode := env_var_or_default('BUILD_MODE', 'auto')

# xcode-mode inputs. Leave blank to auto-detect the single .xcworkspace/
# .xcodeproj and a scheme of the same basename under project_dir.
workspace := env_var_or_default('WORKSPACE', '')
scheme    := env_var_or_default('SCHEME', '')
config    := env_var_or_default('CONFIG', 'Debug')
dest      := env_var_or_default('DEST', 'generic/platform=iOS Simulator')

# Extra flags appended to the build command (e.g. SDK/target for iOS spm builds).
xcode_flags := env_var_or_default('XCODE_BUILD_FLAGS', '')
swift_flags := env_var_or_default('SWIFT_BUILD_FLAGS', '')

# Scratch DerivedData / build dir. Holds both the build output and the index
# store, kept out of the project's own .build / DerivedData.
derived := env_var_or_default('DERIVED', project_dir / ".tmpBuildData")

reader     := justfile_directory() / "index_graph/.build/release/index_graph"
graph_json := justfile_directory() / "index_graph.json"
html       := justfile_directory() / "dependency_graph.html"
md         := justfile_directory() / "migration_plan.md"

# Show the three commands.
_default:
    @just --list

# ── public: the only three you run ────────────────────────────────────────────

# Interactive HTML dependency graph. Rebuilds the index if missing.
tree:
    #!/usr/bin/env bash
    set -euo pipefail
    just _prep
    python3 -m modgraph "{{project_dir}}" --from-index "{{graph_json}}" --graph "{{html}}"
    echo "✓ {{html}}"

# Migration task list, markdown. Rebuilds the index if missing.
list:
    #!/usr/bin/env bash
    set -euo pipefail
    just _prep
    python3 -m modgraph "{{project_dir}}" --from-index "{{graph_json}}" --list "{{md}}"
    echo "✓ {{md}}"

# Live mode: serve the HTML on localhost, hot-reload it whenever `just tree`
# regenerates it, and let cmd+click on a folder pop it open in Xcode (via `xed`).
# Stays running until Ctrl-C. In another terminal, edit code + `just tree` to refresh.
serve port="8765":
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f "{{html}}" ]] || just tree
    python3 -m modgraph.serve --port "{{port}}" --html "{{html}}" --root "{{project_dir}}"

# Both deliverables. Rebuilds the index if missing.
all:
    #!/usr/bin/env bash
    set -euo pipefail
    just _prep
    python3 -m modgraph "{{project_dir}}" --from-index "{{graph_json}}" --graph "{{html}}" --list "{{md}}"
    echo "✓ {{html}}  {{md}}"

# Run the Python test suite (stdlib unittest — no pip deps, no index build).
alias tests := test
test:
    #!/usr/bin/env bash
    set -euo pipefail
    # The cli tests invoke main(), which prints progress to stdout; the unittest
    # report itself goes to stderr, so drop stdout for a clean, readable run.
    python3 -m unittest discover -s "{{justfile_directory()}}/tests" -t "{{justfile_directory()}}" -v 2>&1 >/dev/null

# Python outputs (HTML/markdown/__pycache__) + swift reader output and build
# artifacts. Leaves the target project's own build untouched.
#
# Remove all generated files.
clean:
    @echo "→ removing generated files"
    rm -f "{{html}}" "{{md}}" "{{graph_json}}"
    rm -rf "{{justfile_directory()}}/__pycache__"
    rm -rf "{{justfile_directory()}}/index_graph/.build"
    @echo "✓ cleaned"

# ── private plumbing (hidden from `just --list`) ──────────────────────────────

# Ensure index_graph.json is ready: reuse the existing one, or build it from a
# fresh build of the target project when missing (first run / after `just clean`).
_prep:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -f "{{graph_json}}" ]]; then
        echo "↺ reusing {{graph_json}} (run \`just clean\` to force a rebuild)"
    else
        echo "→ {{graph_json}} missing — building from the project…"
        just _index
    fi

# Build the target project to populate its compiler index store, then resolve
# the graph by USR → index_graph.json.
_index: _build_reader
    #!/usr/bin/env bash
    set -euo pipefail

    proj="{{project_dir}}"
    [[ -d "$proj" ]] || { echo "✗ project_dir not found: $proj" >&2; exit 1; }

    # Resolve build mode.
    mode="{{build_mode}}"
    if [[ "$mode" == "auto" ]]; then
        if ls "$proj"/*.xcworkspace >/dev/null 2>&1 || ls "$proj"/*.xcodeproj >/dev/null 2>&1; then
            mode=xcode
        elif [[ -f "$proj/Package.swift" ]]; then
            mode=spm
        else
            echo "✗ no .xcworkspace/.xcodeproj/Package.swift under $proj (set BUILD_MODE)" >&2
            exit 1
        fi
    fi
    echo "→ build mode: $mode"

    # Wipe the scratch build/index dir so indexing starts clean.
    echo "→ removing {{derived}}"
    rm -rf "{{derived}}"

    if [[ "$mode" == "xcode" ]]; then
        # Auto-detect workspace/project + scheme if not provided.
        ws="{{workspace}}"; sc="{{scheme}}"; proj_flag=""
        if [[ -n "$ws" ]]; then
            proj_flag=(-workspace "$ws")
        elif compgen -G "$proj/*.xcworkspace" >/dev/null; then
            ws=$(basename "$(ls -d "$proj"/*.xcworkspace | head -1)")
            proj_flag=(-workspace "$ws")
        else
            xc=$(basename "$(ls -d "$proj"/*.xcodeproj | head -1)")
            proj_flag=(-project "$xc")
        fi
        if [[ -z "$sc" ]]; then
            schemes=$(cd "$proj" && xcodebuild -list "${proj_flag[@]}" 2>/dev/null \
                | grep -A 100 'Schemes:' | tail -n +2 | grep '^ ' | sed 's/^ *//')
            count=$(echo "$schemes" | grep -c .)
            if [[ $count -eq 1 ]]; then
                sc="$schemes"
                echo "→ auto-detected scheme: $sc"
            elif [[ $count -gt 1 ]]; then
                echo "✗ multiple schemes found — set SCHEME to one of:" >&2
                echo "$schemes" | sed 's/^/    /' >&2
                exit 1
            else
                echo "✗ could not detect scheme — set SCHEME" >&2
                exit 1
            fi
        fi
        echo "→ xcodebuild ${proj_flag[*]} -scheme $sc"

        # arm64-only sim build avoids common x86_64 link failures in deps. A
        # non-zero xcodebuild exit (e.g. a link error) is tolerated because
        # indexing finishes before linking; we then assert the store populated
        # so a real compile failure still hard-fails.
        ( cd "$proj" && xcodebuild clean build \
            "${proj_flag[@]}" -scheme "$sc" -configuration "{{config}}" \
            -destination '{{dest}}' -derivedDataPath "{{derived}}" \
            SWIFT_ENABLE_EXPLICIT_MODULES=NO ARCHS=arm64 ONLY_ACTIVE_ARCH=YES \
            {{xcode_flags}} 2>&1 | xcsift ) || echo "⚠ xcodebuild non-zero — verifying index store…"

        store="{{derived}}/Index.noindex/DataStore"
    else
        # Pure SwiftPM package. swift build emits an index store under the build
        # path by default. Pass SWIFT_BUILD_FLAGS for an iOS SDK/target if the
        # package can't build for the host (e.g. UIKit-only code).
        cfg=$(echo "{{config}}" | tr '[:upper:]' '[:lower:]')
        echo "→ swift build -c $cfg"
        ( cd "$proj" && swift build -c "$cfg" --build-path "{{derived}}" {{swift_flags}} ) \
            || echo "⚠ swift build non-zero — verifying index store…"

        store=$(find "{{derived}}" -type d -path '*/index/store' 2>/dev/null | head -1)
        [[ -n "$store" ]] || { echo "✗ no index store found under {{derived}}" >&2; exit 1; }
    fi

    test -d "$store/v5/units" || { echo "✗ index store not populated — build failed before indexing" >&2; exit 1; }
    echo "✓ index store populated: $store"

    "{{reader}}" "$store" "$proj" "{{graph_json}}"

# Build the Swift index-store reader (cached; fetches apple/indexstore-db once).
_build_reader:
    cd "{{justfile_directory()}}/index_graph" && swift build -c release
