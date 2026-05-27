import Foundation
import IndexStoreDB

// ── index_graph ──────────────────────────────────────────────────────────────
// Reads a Swift/Clang compiler index store and emits a *resolved* folder-level
// dependency graph as JSON.
//
//   index_graph <storePath> <repoRoot> [outJSON]
//
//   storePath  Path to .../Index.noindex/DataStore
//   repoRoot   Project root; only declarations under here are "first-party".
//   outJSON    Where to write the graph JSON (default: stdout).
//
// Every edge is resolved by USR, so a reference to `Foo` points at the *one*
// `Foo` the compiler actually bound — name collisions across folders never
// create phantom edges (the core failure mode of the regex scanner).

// MARK: - JSON shape (consumed by find_leaf_modules.py)

struct Edge: Codable { let src: String; let dst: String; let w: Int }
struct PairTypes: Codable { let src: String; let dst: String; let types: [String] }
struct FileRecord: Codable {
    let folder: String
    let name: String
    let decls: [String]
    let refs: [String]              // referenced type names (name-only, kept for back-compat)
    let ref_owners: [[String]]      // USR-resolved refs as [type name, owner folder] — collision-free
}
struct Graph: Codable {
    let edges: [Edge]
    let pair_types: [PairTypes]
    let folder_decls: [String: [String]]
    let files: [FileRecord]
    let type_owners: [String: [String]]
    let type_kinds: [String: String]   // type name -> "class" | "struct" | "enum" | "protocol" | "typealias"
}

// MARK: - args

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write(Data("usage: index_graph <storePath> <repoRoot> [outJSON]\n".utf8))
    exit(2)
}
let storePath = args[1]
var repoRoot = (args[2] as NSString).standardizingPath
if !repoRoot.hasSuffix("/") { repoRoot += "/" }
let outPath: String? = args.count >= 4 ? args[3] : nil

func log(_ s: String) { FileHandle.standardError.write(Data((s + "\n").utf8)) }

// MARK: - index store

let libPath = "\(shell("xcode-select", "-p"))/Toolchains/XcodeDefault.xctoolchain/usr/lib/libIndexStore.dylib"
func shell(_ cmd: String, _ a: String...) -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = [cmd] + a
    let pipe = Pipe(); p.standardOutput = pipe
    try? p.run(); p.waitUntilExit()
    let d = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: d, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

let dbTmp = (NSTemporaryDirectory() as NSString).appendingPathComponent("index_graph_db_\(getpid())")
// IndexStoreDB's LMDB env requires the database directory to exist up front.
do {
    try FileManager.default.createDirectory(atPath: dbTmp, withIntermediateDirectories: true)
} catch {
    log("could not create db dir \(dbTmp): \(error)")
}
log("db dir: \(dbTmp) exists=\(FileManager.default.fileExists(atPath: dbTmp))")
log("store exists=\(FileManager.default.fileExists(atPath: storePath))  v5 exists=\(FileManager.default.fileExists(atPath: storePath + "/v5"))")
let lib: IndexStoreLibrary
let db: IndexStoreDB
do {
    lib = try IndexStoreLibrary(dylibPath: libPath)
    db = try IndexStoreDB(
        storePath: storePath,
        databasePath: dbTmp,
        library: lib,
        waitUntilDoneInitializing: true,
        // Must be read-WRITE: readonly mode assumes a pre-built DB and skips
        // creating the LMDB dir, failing with mdb_env_open ENOENT. We want it to
        // import the index store into a fresh scratch DB, so let it create dirs.
        readonly: false,
        // Must listen to unit events so it actually ingests the store's units;
        // waitUntilDoneInitializing then blocks until that ingestion completes.
        listenToUnitEvents: true
    )
} catch {
    log("failed to open index store: \(error)\nlib=\(libPath)\nstore=\(storePath)")
    exit(1)
}
log("index store opened: \(storePath)")
// Force synchronous ingestion of all units already present in the store.
db.pollForUnitChangesAndWait()
log("polled units; ingestion complete")

// MARK: - first-party filter + folder mapping

// Exclude system, build-output, and SPM-checkout sources. First-party local
// package sources live under repoRoot but NOT under these → kept.
let excludedFragments = ["/.tmpBuildData/", "/DerivedData/", "/SourcePackages/", "/.build/", "/checkouts/"]
func isFirstParty(_ path: String) -> Bool {
    guard path.hasPrefix(repoRoot) else { return false }
    for f in excludedFragments where path.contains(f) { return false }
    return true
}
// POSIX folder relative to repoRoot; root-level files bucket into ".".
func relFolder(_ path: String) -> String {
    let rel = String(path.dropFirst(repoRoot.count))
    guard let slash = rel.lastIndex(of: "/") else { return "." }
    let folder = String(rel[..<slash])
    return folder.isEmpty ? "." : folder
}

let typeKinds: Set<IndexSymbolKind> = [.class, .struct, .enum, .protocol, .typealias]

func kindString(_ k: IndexSymbolKind) -> String {
    switch k {
    case .class:     return "class"
    case .struct:    return "struct"
    case .enum:      return "enum"
    case .protocol:  return "protocol"
    case .typealias: return "typealias"
    default:         return "type"
    }
}

// MARK: - phase 1: collect first-party type declarations (canonical)

struct Decl { let name: String; let folder: String }
var declByUSR: [String: Decl] = [:]                 // usr -> declaring type
var folderDecls: [String: Set<String>] = [:]        // folder -> type names
var typeOwners: [String: Set<String>] = [:]         // name -> folders
var fileDecls: [String: Set<String>] = [:]          // path -> type names declared
var typeKindMap: [String: String] = [:]             // name -> kind (class/struct/enum/protocol/typealias)

// Enumerate every symbol name in the store, then resolve each to its canonical
// occurrences. (An empty `containing:` pattern matches nothing, so we can't use
// it to sweep everything — forEachSymbolName is the real enumeration entry point.)
// Collect all names FIRST — calling another read query inside the
// forEachSymbolName callback nests LMDB read txns on the same thread and
// crashes with MDB_BAD_RSLOT. So buffer the names, then resolve outside.
var names: [String] = []
db.forEachSymbolName { names.append($0); return true }
log("symbol names scanned: \(names.count)")
for name in names {
    db.forEachCanonicalSymbolOccurrence(byName: name) { occ in
        let sym = occ.symbol
        guard typeKinds.contains(sym.kind) else { return true }
        guard occ.roles.contains(.definition) || occ.roles.contains(.declaration) else { return true }
        let path = occ.location.path
        guard !occ.location.isSystem, isFirstParty(path) else { return true }
        let folder = relFolder(path)
        declByUSR[sym.usr] = Decl(name: sym.name, folder: folder)
        folderDecls[folder, default: []].insert(sym.name)
        typeOwners[sym.name, default: []].insert(folder)
        fileDecls[path, default: []].insert(sym.name)
        // First kind wins; same-named types rarely differ in kind, and one
        // label per name is enough for the type-level view.
        if typeKindMap[sym.name] == nil { typeKindMap[sym.name] = kindString(sym.kind) }
        return true
    }
}
log("first-party type decls: \(declByUSR.count) across \(folderDecls.count) folders")

// MARK: - phase 2: resolve references to those types (by USR)

var pairTypes: [String: [String: Set<String>]] = [:]  // src -> dst -> {type names}
var fileRefs: [String: Set<String>] = [:]             // path -> referenced type names
// path -> {"name\tfolder"} — every ref resolved to the exact declaring type's
// owner folder by USR. Lets the type-view match references to the one type the
// compiler actually bound, instead of every same-named type across the app.
var fileRefPairs: [String: Set<String>] = [:]

for (usr, decl) in declByUSR {
    for occ in db.occurrences(ofUSR: usr, roles: .reference) {
        let path = occ.location.path
        guard !occ.location.isSystem, isFirstParty(path) else { continue }
        let src = relFolder(path)
        fileRefs[path, default: []].insert(decl.name)
        fileRefPairs[path, default: []].insert(decl.name + "\t" + decl.folder)
        guard src != decl.folder else { continue }     // intra-folder ref: not an edge
        pairTypes[src, default: [:]][decl.folder, default: []].insert(decl.name)
    }
}

// MARK: - assemble JSON

var edges: [Edge] = []
var pairList: [PairTypes] = []
for (src, dsts) in pairTypes {
    for (dst, types) in dsts {
        edges.append(Edge(src: src, dst: dst, w: types.count))
        pairList.append(PairTypes(src: src, dst: dst, types: types.sorted()))
    }
}

// One FileRecord per source file that declares or references a first-party type.
var allPaths = Set(fileDecls.keys)
allPaths.formUnion(fileRefs.keys)
var files: [FileRecord] = []
for path in allPaths {
    let owners: [[String]] = (fileRefPairs[path] ?? []).sorted().map { pair in
        let parts = pair.split(separator: "\t", maxSplits: 1).map(String.init)
        return [parts[0], parts.count > 1 ? parts[1] : ""]
    }
    files.append(FileRecord(
        folder: relFolder(path),
        name: (path as NSString).lastPathComponent,
        decls: (fileDecls[path] ?? []).sorted(),
        refs: (fileRefs[path] ?? []).sorted(),
        ref_owners: owners
    ))
}

let graph = Graph(
    edges: edges.sorted { $0.src == $1.src ? $0.dst < $1.dst : $0.src < $1.src },
    pair_types: pairList,
    folder_decls: folderDecls.mapValues { $0.sorted() },
    files: files,
    type_owners: typeOwners.mapValues { $0.sorted() },
    type_kinds: typeKindMap
)

let enc = JSONEncoder()
enc.outputFormatting = [.sortedKeys]
let data = try enc.encode(graph)
if let outPath {
    try data.write(to: URL(fileURLWithPath: outPath))
    log("wrote \(data.count) bytes → \(outPath)")
    log("edges=\(edges.count) folders=\(folderDecls.count) types=\(declByUSR.count) files=\(files.count)")
} else {
    FileHandle.standardOutput.write(data)
}
