# ObjC support — design

**Status:** proposal, pre-implementation. **Goal:** mixed Swift/ObjC monoliths
(the projects that most need an SPM migration) get truthful edges — today any
dependency that exists only in ObjC is invisible, so the plan can claim a
folder is "extractable now" while an unseen `#import` pins it.

## The surprising baseline

The Swift reader has **no Swift-only filter**. It filters occurrences by
first-party *path* (`IndexGraphCore.isFirstParty`) and by *symbol kind*
(`typeKinds`/`fileEdgeKinds` in `main.swift`) — both language-agnostic.
`IndexStoreDB` ingests **clang units** (ObjC) exactly like Swift ones, and an
ordinary `xcodebuild` run indexes ObjC sources by default (index-while-building
is on for clang too). `IndexSymbolKind.class/.protocol/...` and the
`.childOf`/`.containedBy` relations exist for ObjC occurrences as well; only
USR spelling differs (`c:objc(cs)Foo` vs `s:...`), and the reader treats USRs
as opaque keys.

**Hypothesis: ObjC type declarations and refs already flow into
`index_graph.json` on mixed projects.** Nobody has verified it, and the
tooling around the reader assumes Swift in several places. So the epic is
*verify, then close the gaps* — not *build a second reader*.

## Phase 0 — spike: prove what the store already gives us (no product code)

Build a tiny mixed fixture (xcodeproj or two SPM targets — SPM forbids mixing
languages *within* a target, which is itself representative):

* `ObjCKit/` — `FOOLegacyStore.{h,m}` declaring a class + a protocol, a
  category, and a `#import`-only dependency on a second ObjC class.
* `SwiftKit/` — Swift class referencing `FOOLegacyStore` via bridging.
* Swift→ObjC, ObjC→ObjC, and ObjC→Swift (via `-Swift.h`) references.

Run the real pipeline; inspect `index_graph.json`. Questions the spike must
answer, each becoming a test later:

1. Do ObjC class/protocol decls land in `folder_decls` with sensible kinds?
2. Do Swift→ObjC refs resolve by USR (bridged names: `FOOLegacyStore` is one
   symbol with one USR — does the Swift reference's USR match the clang
   decl's, or does the store record a distinct "bridged" symbol)?
3. Do ObjC→Swift refs through the generated `-Swift.h` resolve to the Swift
   USR or dead-end at the generated header (which `isFirstParty` would reject
   — it lives in DerivedData)?
4. Headers: a decl usually appears in the `.h`, its definition in the `.m` —
   which path does the canonical occurrence carry, and is folder attribution
   stable when headers live apart from implementations (`include/` layouts)?
5. Categories/extensions: does the parent-chain walk
   (`IndexGraphCore.containingType`) terminate correctly for `@interface
   Foo (Bar)` members?

## Phase 1 — reader correctness (driven by spike findings)

Expected small fixes, all in `index_graph/` + `IndexGraphCore`:

* Possibly admit additional `IndexSymbolKind`s (e.g. `.extension`-like clang
  kinds, `enumConstant` for `NS_ENUM`) to `typeKinds`/`fileEdgeKinds`.
* Possibly resolve decl-in-header vs definition-in-impl so a type's
  `src_file`/folder attribution prefers the implementation file (or document
  header-folder attribution as intended).
* Bridged-reference normalization if (2)/(3) show split USRs: map the
  generated-header occurrence back to the Swift USR via the store's
  relations rather than dropping it.
* Schema: no shape change expected (types/edges are name+folder based). If a
  per-type `lang` tag proves useful for the UI, it's an additive optional
  field — no `schema_version` bump.

## Phase 2 — tooling around the reader stops assuming Swift

* `churn.py`: count `.m`/`.mm`/`.h` alongside `.swift` (it weights compile
  cost; ObjC compiles too). One-line constant + tests.
* `build_times.py`: `-stats-output-dir` is swift-frontend only — ObjC compile
  work is invisible to the *measured* path. Document it; the type-count proxy
  already covers ObjC types once the reader emits them. (clang `-ftime-trace`
  is the someday-fix; out of scope.)
* Migration prompts: a step whose folder contains `.h/.m` needs ObjC-specific
  instructions (umbrella header, module map, `publicHeadersPath`); gate on the
  folder's file inventory (`files` already carries names).
* UI: `type_kinds` may gain ObjC kinds; legend copy. No structural work.
* Regex fallback (`--ext .swift`): explicitly out of scope — stays Swift-only,
  banner already warns it's degraded.

## Phase 3 — fixtures, e2e, CI

* Extend `tests/test_e2e_pipeline.py`'s toy package with an ObjC target
  (SPM C-family target) and assert the cross-language edge exists — the same
  gated/manual pattern as today (`MODGRAPH_E2E=1`).
* Python fixtures gain ObjC type names in `folder_decls`/`type_kinds` so the
  plan/divide/build paths prove they're language-blind (they should be —
  they're string-keyed).

## Sizing

Phase 0 a day; phases 1–3 each PR-sized once the spike removes the unknowns.
The risk concentrates in (2)/(3) — bridged USR identity. If the store splits
bridged symbols irreparably, cross-language edges need a name-based merge pass
with collision handling, and that's the point to stop and re-scope rather than
ship phantom-edge-shaped guesses — the tool's whole identity is "edges are
real".
