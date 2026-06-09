// USR parent-chain walking, factored out of main.swift so it can be unit-tested
// without an index store. Pure: given a way to ask "is this USR a first-party
// declaration?" and a parent map, it walks up.

/// Walk the USR parent chain from `usr` until reaching a USR that satisfies
/// `isDeclared` (a first-party type we track), and return it; nil if the chain
/// runs out. Used to attribute a reference (to a method, property, nested type,
/// …) back to the top-level type that owns it.
///
/// - Parameters:
///   - usr: the starting USR.
///   - isDeclared: membership test — true if the USR is a tracked declaration.
///     Passed as a closure so callers don't have to materialise a key set on
///     every call inside a hot loop.
///   - parent: USR → its parent USR.
///
/// Cycles are guarded with a `seen` set — the index store shouldn't produce
/// them, but it's cheap insurance against an infinite loop.
public func containingType(
    _ usr: String,
    isDeclared: (String) -> Bool,
    parent: [String: String]
) -> String? {
    var seen = Set<String>()
    var cur: String? = usr
    while let c = cur, !seen.contains(c) {
        seen.insert(c)
        if isDeclared(c) { return c }
        cur = parent[c]
    }
    return nil
}
