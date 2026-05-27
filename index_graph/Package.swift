// swift-tools-version:5.9
import PackageDescription

// Reads an Xcode/Swift compiler **index store** and emits a resolved, folder-level
// dependency graph as JSON for find_leaf_modules.py to render. Uses Apple's
// IndexStoreDB so every reference is resolved to the *exact* declaration (by USR),
// eliminating the name-collision false edges the regex scanner produces.
let package = Package(
    name: "index_graph",
    platforms: [.macOS(.v13)],
    dependencies: [
        // Branch is pinned to match the local Swift toolchain (6.3). If this fails
        // to resolve, swap to .branch("main").
        .package(url: "https://github.com/apple/indexstore-db.git", branch: "release/6.3"),
    ],
    targets: [
        .executableTarget(
            name: "index_graph",
            dependencies: [
                .product(name: "IndexStoreDB", package: "indexstore-db"),
            ]
        ),
    ]
)
