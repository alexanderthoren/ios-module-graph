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
struct FileEdge: Codable {
    let src: String                 // referencing file, path relative to repoRoot
    let dst: String                 // declaring file, path relative to repoRoot
    let w: Int                      // total occurrences
    let symbols: [String]           // referenced symbol names (top-N, capped server-side)
}
struct TypeEdge: Codable {
    // Both endpoints are first-party TYPE declarations. Resolved via the
    // .containedBy relations index-store emits on every reference occurrence:
    // for each non-decl ref, we walk up from the contextual entity until we
    // hit a tracked type — that's the src; the dst is the referenced symbol's
    // owning type (the symbol's own containing type, or the symbol itself if
    // it IS a type). Lets the type-view skip file nodes and connect class to
    // class / struct to struct directly.
    let src: String                 // type name as "<name>\t<owner_folder>" (collision-safe key)
    let dst: String
    let w: Int
    let symbols: [String]           // top-N referenced symbol names (member or type)
    let src_file: String            // path of file declaring src type (rel to repoRoot)
    let dst_file: String            // path of file declaring dst type
}
// Version of the JSON contract between this Swift producer and the Python
// consumer (modgraph/index_loader.py). BUMP THIS whenever the emitted shape
// changes incompatibly, and bump INDEX_SCHEMA_VERSION in index_loader.py to
// match — the loader hard-fails on a mismatch instead of crashing cryptically.
let schemaVersion = 1

struct Graph: Codable {
    let schema_version: Int
    let edges: [Edge]
    let pair_types: [PairTypes]
    let folder_decls: [String: [String]]
    let files: [FileRecord]
    let type_owners: [String: [String]]
    let type_kinds: [String: String]   // type name -> "class" | "struct" | "enum" | "protocol" | "typealias"
    let file_edges: [FileEdge]
    // Precise type→type edges. Same data as file_edges but lifted one level:
    // src/dst are the containing TYPES of the ref site / referenced symbol,
    // not their files. Type-view consumes this directly so a class can connect
    // to a struct without a file box in between.
    let type_edges: [TypeEdge]
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
// Symbol kinds tracked for FILE-LEVEL edges only — vars, funcs, methods, props,
// inits, subscripts. They aren't first-class graph nodes (the folder graph is
// type-driven on purpose), but they DO couple files together via property/method
// access and must surface in the type-view so the user sees the real edges.
let fileEdgeKinds: Set<IndexSymbolKind> = [
    .class, .struct, .enum, .protocol, .typealias,
    .function, .instanceMethod, .staticMethod, .classMethod,
    .constructor, .instanceProperty, .classProperty, .staticProperty,
    .variable, .enumConstant,
]

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
// File-level decls: USR -> (declaring file path, symbol name). Covers types AND
// non-types so we can later resolve any reference's target file by USR.
struct FileDecl { let path: String; let name: String }
var fileDeclByUSR: [String: FileDecl] = [:]
// Containment chain: usr -> immediate parent usr (via .childOf / .containedBy).
// Lets us resolve "ref happens inside method M of type X" to type X by walking
// the chain up to the first tracked type USR.
var parentByUSR: [String: String] = [:]
var kindByUSR: [String: IndexSymbolKind] = [:]

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
        guard fileEdgeKinds.contains(sym.kind) else { return true }
        guard occ.roles.contains(.definition) || occ.roles.contains(.declaration) else { return true }
        let path = occ.location.path
        guard !occ.location.isSystem, isFirstParty(path) else { return true }
        // Type-only bookkeeping: feeds the folder-level graph and type-view labels.
        if typeKinds.contains(sym.kind) {
            let folder = relFolder(path)
            declByUSR[sym.usr] = Decl(name: sym.name, folder: folder)
            folderDecls[folder, default: []].insert(sym.name)
            typeOwners[sym.name, default: []].insert(folder)
            fileDecls[path, default: []].insert(sym.name)
            // First kind wins; same-named types rarely differ in kind, and one
            // label per name is enough for the type-level view.
            if typeKindMap[sym.name] == nil { typeKindMap[sym.name] = kindString(sym.kind) }
        }
        // File-level bookkeeping: types + non-types alike, used for file_edges.
        // First definition wins — generated/synthesized re-decls aren't useful targets.
        if fileDeclByUSR[sym.usr] == nil {
            fileDeclByUSR[sym.usr] = FileDecl(path: path, name: sym.name)
        }
        // Containment chain. The relation `.childOf` / `.containedBy` on this
        // occurrence points to the immediate parent (extension/type/file). We
        // store kind/usr for both sides so the chain walk later can climb to
        // the first type without re-querying the store.
        kindByUSR[sym.usr] = sym.kind
        for rel in occ.relations {
            if rel.roles.contains(.childOf) || rel.roles.contains(.containedBy) {
                parentByUSR[sym.usr] = rel.symbol.usr
                if kindByUSR[rel.symbol.usr] == nil {
                    kindByUSR[rel.symbol.usr] = rel.symbol.kind
                }
                break
            }
        }
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
    // SymbolRole.all + filter-out-decl matches every non-declaration occurrence
    // (reference, read, write, call, baseOf, extendedBy, calledBy, containedBy,
    // specializationOf, ibTypeOf, implicit, …). Previously we asked only for
    // `.reference` and lost cases like a method-return type annotation whose
    // sole role is `.reference + .containedBy` — fine — but also property reads
    // whose roles are `.read` only, which the narrower query dropped.
    for occ in db.occurrences(ofUSR: usr, roles: .all) {
        if occ.roles.contains(.definition) || occ.roles.contains(.declaration) { continue }
        let path = occ.location.path
        guard !occ.location.isSystem, isFirstParty(path) else { continue }
        let src = relFolder(path)
        fileRefs[path, default: []].insert(decl.name)
        fileRefPairs[path, default: []].insert(decl.name + "\t" + decl.folder)
        guard src != decl.folder else { continue }     // intra-folder ref: not an edge
        pairTypes[src, default: [:]][decl.folder, default: []].insert(decl.name)
    }
}

// MARK: - phase 3: resolve file-to-file edges (all symbol kinds, by USR)

// (src_path, dst_path) -> (weight, symbol-name -> count). symbol counts let us
// cap the per-edge symbol list to the top-N callers on the Python side without
// losing the long tail's weight contribution.
var fileEdgeWeight: [String: [String: Int]] = [:]
var fileEdgeSymCounts: [String: [String: [String: Int]]] = [:]

func relPath(_ p: String) -> String {
    guard p.hasPrefix(repoRoot) else { return p }
    return String(p.dropFirst(repoRoot.count))
}

for (usr, fd) in fileDeclByUSR {
    for occ in db.occurrences(ofUSR: usr, roles: .all) {
        if occ.roles.contains(.definition) || occ.roles.contains(.declaration) { continue }
        let path = occ.location.path
        guard !occ.location.isSystem, isFirstParty(path) else { continue }
        if path == fd.path { continue }                  // intra-file: not an edge
        fileEdgeWeight[path, default: [:]][fd.path, default: 0] += 1
        fileEdgeSymCounts[path, default: [:]][fd.path, default: [:]][fd.name, default: 0] += 1
    }
}

var fileEdges: [FileEdge] = []
let maxSymsPerEdge = 12
for (src, dsts) in fileEdgeWeight {
    for (dst, w) in dsts {
        let counts = fileEdgeSymCounts[src]?[dst] ?? [:]
        // Top-N by usage; ties broken by name for determinism.
        let topSyms = counts.sorted {
            $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value
        }.prefix(maxSymsPerEdge).map { $0.key }
        fileEdges.append(FileEdge(src: relPath(src), dst: relPath(dst), w: w, symbols: topSyms))
    }
}
log("file_edges: \(fileEdges.count)")

// MARK: - phase 4: type→type edges (containing-type-resolved)

// Walks the parent chain until it hits a USR registered in declByUSR (first-party
// type). Cycles are guarded with a `seen` set — defensive, the index store
// shouldn't produce them but cheap insurance.
func containingType(_ usr: String) -> String? {
    var seen = Set<String>()
    var cur: String? = usr
    while let c = cur, !seen.contains(c) {
        seen.insert(c)
        if declByUSR[c] != nil { return c }
        cur = parentByUSR[c]
    }
    return nil
}

// (srcTypeUSR, dstTypeUSR) -> total weight + symbol-name -> count.
var typeEdgeWeight: [String: [String: Int]] = [:]
var typeEdgeSyms: [String: [String: [String: Int]]] = [:]

for (usr, fd) in fileDeclByUSR {
    // dstType: the type that owns the referenced symbol. If the symbol itself
    // is a type, that's the answer; otherwise walk the parent chain.
    let dstType: String?
    if declByUSR[usr] != nil { dstType = usr }
    else if let p = parentByUSR[usr] { dstType = containingType(p) }
    else { dstType = nil }
    guard let dst = dstType else { continue }

    for occ in db.occurrences(ofUSR: usr, roles: .all) {
        if occ.roles.contains(.definition) || occ.roles.contains(.declaration) { continue }
        let path = occ.location.path
        guard !occ.location.isSystem, isFirstParty(path) else { continue }
        // Resolve the ref site's containing type via the occurrence's relations.
        var containerUSR: String? = nil
        for rel in occ.relations {
            if rel.roles.contains(.childOf) || rel.roles.contains(.containedBy) {
                containerUSR = rel.symbol.usr
                // Backfill parent/kind for transitive walks (the container's
                // own canonical occurrence may not have been seen yet, e.g.
                // when the container is itself an extension).
                if let cu = containerUSR {
                    if kindByUSR[cu] == nil { kindByUSR[cu] = rel.symbol.kind }
                }
                break
            }
        }
        guard let cu = containerUSR, let src = containingType(cu), src != dst else { continue }
        typeEdgeWeight[src, default: [:]][dst, default: 0] += 1
        typeEdgeSyms[src, default: [:]][dst, default: [:]][fd.name, default: 0] += 1
    }
}

var typeEdges: [TypeEdge] = []
let maxTypeSymsPerEdge = 12
for (srcUSR, dsts) in typeEdgeWeight {
    guard let srcDecl = declByUSR[srcUSR] else { continue }
    // Type's declaring file lookup: scan fileDecls for the type name in its
    // owner folder. Tracked indirectly via declByUSR.folder + fileDecls inversion;
    // for cheap lookup, derive at runtime.
    var srcFile = ""
    for (path, names) in fileDecls where names.contains(srcDecl.name) {
        if relFolder(path) == srcDecl.folder { srcFile = relPath(path); break }
    }
    for (dstUSR, w) in dsts {
        guard let dstDecl = declByUSR[dstUSR] else { continue }
        var dstFile = ""
        for (path, names) in fileDecls where names.contains(dstDecl.name) {
            if relFolder(path) == dstDecl.folder { dstFile = relPath(path); break }
        }
        let counts = typeEdgeSyms[srcUSR]?[dstUSR] ?? [:]
        let topSyms = counts.sorted {
            $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value
        }.prefix(maxTypeSymsPerEdge).map { $0.key }
        typeEdges.append(TypeEdge(
            src: srcDecl.name + "\t" + srcDecl.folder,
            dst: dstDecl.name + "\t" + dstDecl.folder,
            w: w, symbols: topSyms,
            src_file: srcFile, dst_file: dstFile
        ))
    }
}
log("type_edges: \(typeEdges.count)")

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
    schema_version: schemaVersion,
    edges: edges.sorted { $0.src == $1.src ? $0.dst < $1.dst : $0.src < $1.src },
    pair_types: pairList,
    folder_decls: folderDecls.mapValues { $0.sorted() },
    files: files,
    type_owners: typeOwners.mapValues { $0.sorted() },
    type_kinds: typeKindMap,
    file_edges: fileEdges.sorted { $0.src == $1.src ? $0.dst < $1.dst : $0.src < $1.src },
    type_edges: typeEdges.sorted { $0.src == $1.src ? $0.dst < $1.dst : $0.src < $1.src }
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
