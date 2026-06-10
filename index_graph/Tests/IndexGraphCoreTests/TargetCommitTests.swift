import XCTest
@testable import IndexGraphCore

final class TargetCommitTests: XCTestCase {
    private let sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    func testCleanRepoProducesCommit() {
        let r = targetCommit(sha: sha + "\n", statusPorcelain: "", subject: "feat: x\n")
        XCTAssertEqual(r, TargetCommit(sha: sha, dirty: false, subject: "feat: x"))
    }

    func testNonBlankStatusMeansDirty() {
        let r = targetCommit(sha: sha, statusPorcelain: " M Sources/A.swift\n", subject: "s")
        XCTAssertEqual(r?.dirty, true)
    }

    func testWhitespaceOnlyStatusMeansClean() {
        let r = targetCommit(sha: sha, statusPorcelain: "  \n", subject: "s")
        XCTAssertEqual(r?.dirty, false)
    }

    func testEmptyShaReturnsNil() {
        // Not a git repo: rev-parse writes nothing to stdout.
        XCTAssertNil(targetCommit(sha: "", statusPorcelain: "", subject: ""))
    }

    func testLiteralHeadReturnsNil() {
        // Repo with no commits: `git rev-parse HEAD` echoes the literal "HEAD".
        XCTAssertNil(targetCommit(sha: "HEAD\n", statusPorcelain: "", subject: ""))
    }

    func testShortNonShaReturnsNil() {
        XCTAssertNil(targetCommit(sha: "abc", statusPorcelain: "", subject: ""))
    }

    func testAbbreviatedShaIsAccepted() {
        let r = targetCommit(sha: "a1b2c3d", statusPorcelain: "", subject: "s")
        XCTAssertEqual(r?.sha, "a1b2c3d")
    }
}
