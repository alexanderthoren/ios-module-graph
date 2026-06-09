import XCTest
@testable import IndexGraphCore

// repoRoot always ends in "/" (main.swift normalizes it), so tests use that form.
private let root = "/Users/me/proj/"

final class IsFirstPartyTests: XCTestCase {
    func testFirstPartySourceUnderRootIsKept() {
        XCTAssertTrue(isFirstParty("/Users/me/proj/App/View.swift", repoRoot: root))
    }

    func testPathOutsideRootIsRejected() {
        XCTAssertFalse(isFirstParty("/Users/other/lib/Thing.swift", repoRoot: root))
    }

    func testSiblingPrefixIsNotMistakenForRoot() {
        // Trailing "/" on root prevents "/Users/me/proj-tools" matching the prefix.
        XCTAssertFalse(isFirstParty("/Users/me/proj-tools/X.swift", repoRoot: root))
    }

    func testBuildAndCheckoutFragmentsAreExcluded() {
        for frag in [".build", "checkouts", "DerivedData", "SourcePackages", ".tmpBuildData"] {
            let p = "/Users/me/proj/\(frag)/Dep.swift"
            XCTAssertFalse(isFirstParty(p, repoRoot: root), "should exclude \(frag)")
        }
    }

    func testNestedCheckoutDeepUnderRootIsExcluded() {
        let p = "/Users/me/proj/.build/checkouts/SomeLib/Sources/Lib.swift"
        XCTAssertFalse(isFirstParty(p, repoRoot: root))
    }

    func testLocalSpmPackageSourcesAreKept() {
        // A local package's Sources live under root but clear of excluded fragments.
        let p = "/Users/me/proj/Packages/Core/Sources/Core/Core.swift"
        XCTAssertTrue(isFirstParty(p, repoRoot: root))
    }

    func testCustomExcludedFragmentsOverrideDefault() {
        let p = "/Users/me/proj/Generated/G.swift"
        XCTAssertTrue(isFirstParty(p, repoRoot: root))
        XCTAssertFalse(isFirstParty(p, repoRoot: root, excludedFragments: ["/Generated/"]))
    }
}

final class RelFolderTests: XCTestCase {
    func testNestedFileReturnsItsFolder() {
        XCTAssertEqual(relFolder("/Users/me/proj/App/UI/View.swift", repoRoot: root), "App/UI")
    }

    func testTopLevelFileBucketsIntoDot() {
        XCTAssertEqual(relFolder("/Users/me/proj/main.swift", repoRoot: root), ".")
    }

    func testSingleFolderDepth() {
        XCTAssertEqual(relFolder("/Users/me/proj/Core/Core.swift", repoRoot: root), "Core")
    }
}

final class RelPathTests: XCTestCase {
    func testStripsRootPrefix() {
        XCTAssertEqual(relPath("/Users/me/proj/App/View.swift", repoRoot: root), "App/View.swift")
    }

    func testPathOutsideRootReturnedUnchanged() {
        XCTAssertEqual(relPath("/elsewhere/X.swift", repoRoot: root), "/elsewhere/X.swift")
    }
}
