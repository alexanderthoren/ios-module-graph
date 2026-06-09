// Pure path-filtering logic for the index reader, factored out of main.swift so
// it can be unit-tested without standing up an index store. No dependency on
// IndexStoreDB — just String/Foundation — so the test target stays light.
//
// These decide which index records are *first-party* (under the repo, but not
// build output or fetched package checkouts) and how a file path maps to the
// folder id used as a graph node. Getting them wrong silently drops real code
// or pulls in dependencies' sources, so they are exactly what wants tests.

/// Path fragments that mark a file as build output or a fetched dependency,
/// never first-party source — even though they may sit under the repo root.
public let defaultExcludedFragments: [String] = [
    "/.tmpBuildData/", "/DerivedData/", "/SourcePackages/", "/.build/", "/checkouts/",
]

/// True when `path` is first-party source: under `repoRoot` and clear of every
/// excluded fragment. `repoRoot` is expected to end in "/" (as main.swift
/// normalizes it) so the prefix test can't match a sibling like `/repo-foo`.
public func isFirstParty(
    _ path: String,
    repoRoot: String,
    excludedFragments: [String] = defaultExcludedFragments
) -> Bool {
    guard path.hasPrefix(repoRoot) else { return false }
    for f in excludedFragments where path.contains(f) { return false }
    return true
}

/// POSIX folder of `path` relative to `repoRoot`; root-level files bucket into
/// ".". Assumes `path` is under `repoRoot` (callers gate with `isFirstParty`).
public func relFolder(_ path: String, repoRoot: String) -> String {
    let rel = String(path.dropFirst(repoRoot.count))
    guard let slash = rel.lastIndex(of: "/") else { return "." }
    let folder = String(rel[..<slash])
    return folder.isEmpty ? "." : folder
}

/// `path` relative to `repoRoot`, or the path unchanged if it isn't under it.
public func relPath(_ path: String, repoRoot: String) -> String {
    guard path.hasPrefix(repoRoot) else { return path }
    return String(path.dropFirst(repoRoot.count))
}
