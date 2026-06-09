import XCTest
@testable import IndexGraphCore

final class ContainingTypeTests: XCTestCase {
    // A small synthetic USR graph: a method nested in a struct nested in a class.
    //   m (method) → S (struct) → C (class)
    // Only S and C are "declared" (first-party types we track); m is not.
    private let parent = ["m": "S", "S": "C", "C": "root"]
    private func declared(_ set: Set<String>) -> (String) -> Bool { { set.contains($0) } }

    func testReturnsUsrItselfWhenAlreadyDeclared() {
        let r = containingType("S", isDeclared: declared(["S", "C"]), parent: parent)
        XCTAssertEqual(r, "S")
    }

    func testWalksUpToNearestDeclaredAncestor() {
        // m isn't declared; its nearest declared ancestor is S.
        let r = containingType("m", isDeclared: declared(["S", "C"]), parent: parent)
        XCTAssertEqual(r, "S")
    }

    func testSkipsUndeclaredIntermediateToReachDeclared() {
        // Neither m nor S declared → walk on to C.
        let r = containingType("m", isDeclared: declared(["C"]), parent: parent)
        XCTAssertEqual(r, "C")
    }

    func testReturnsNilWhenNoAncestorIsDeclared() {
        let r = containingType("m", isDeclared: declared([]), parent: parent)
        XCTAssertNil(r)
    }

    func testReturnsNilWhenChainEndsBeforeADeclaration() {
        // "x" has no parent entry and isn't declared.
        let r = containingType("x", isDeclared: declared(["S", "C"]), parent: parent)
        XCTAssertNil(r)
    }

    func testCycleDoesNotInfiniteLoop() {
        // A ⇄ B with neither declared: must terminate and return nil.
        let cyclic = ["A": "B", "B": "A"]
        let r = containingType("A", isDeclared: declared([]), parent: cyclic)
        XCTAssertNil(r)
    }

    func testCycleStillFindsADeclaredMember() {
        let cyclic = ["A": "B", "B": "A"]
        let r = containingType("A", isDeclared: declared(["B"]), parent: cyclic)
        XCTAssertEqual(r, "B")
    }
}
