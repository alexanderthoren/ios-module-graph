import Foundation

/// The target project's git state at index time, embedded into the emitted
/// JSON. Downstream consumers (staleness warnings, graph diffs) need the
/// commit the graph *describes* — the HEAD at render time may have moved on.
public struct TargetCommit: Codable, Equatable {
    public let sha: String
    public let dirty: Bool
    public let subject: String

    public init(sha: String, dirty: Bool, subject: String) {
        self.sha = sha
        self.dirty = dirty
        self.subject = subject
    }
}

/// Build a `TargetCommit` from raw `git` outputs. Pure — the caller runs git —
/// so the interpretation is unit-testable without a repository.
///
/// - `sha`: `git rev-parse HEAD` stdout. Must look like a commit hash
///   (≥7 lowercase hex chars once trimmed) — an empty repo prints the literal
///   `HEAD`, and a non-repo prints nothing; both yield `nil` so the JSON field
///   is omitted rather than carrying garbage.
/// - `statusPorcelain`: `git status --porcelain` stdout; any non-blank content
///   means the working tree is dirty.
/// - `subject`: `git log -1 --pretty=%s` stdout.
public func targetCommit(sha: String, statusPorcelain: String, subject: String) -> TargetCommit? {
    let trimmed = sha.trimmingCharacters(in: .whitespacesAndNewlines)
    guard trimmed.count >= 7, trimmed.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
        return nil
    }
    return TargetCommit(
        sha: trimmed,
        dirty: !statusPorcelain.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
        subject: subject.trimmingCharacters(in: .whitespacesAndNewlines)
    )
}
