// Unit tests for modgraph/templates/graph_logic.js — the pure helpers extracted
// from template.html. Run with Node's built-in runner (no npm deps):
//
//     node --test tests/js/
//
// The closure builders take a vis-network DataSet, but only ever call .get()
// returning an array of {from,to} edges, so a one-method stub stands in for it.
const test = require('node:test');
const assert = require('node:assert');
const {
  escapeHtml, fmtDur, buildRebuildClosure, buildDependencyClosure, tarjanSccs,
  migrationPlanOrder, decodePayload, resourcesUnder,
} = require('../../modgraph/templates/graph_logic.js');

// Just the ordered list of folder-groups from a plan (drops decoration).
const planFolders = steps => steps.map(s => s.folders);

// Normalise SCC output for order-independent comparison: each component sorted,
// then the list of components sorted by its first member.
const normSccs = sccs =>
  sccs.map(c => [...c].sort()).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));

const ds = edges => ({ get: () => edges });
const sorted = set => [...set].sort();

test('escapeHtml escapes the five HTML-significant characters', () => {
  assert.strictEqual(
    escapeHtml(`<a href="x" data='y'>&</a>`),
    '&lt;a href=&quot;x&quot; data=&#39;y&#39;&gt;&amp;&lt;/a&gt;',
  );
});

test('escapeHtml coerces non-strings via String()', () => {
  assert.strictEqual(escapeHtml(42), '42');
  assert.strictEqual(escapeHtml(null), 'null');
});

test('fmtDur formats sub-second, seconds, minutes, hours', () => {
  assert.strictEqual(fmtDur(0.42), '0.4s');
  assert.strictEqual(fmtDur(42), '42s');
  assert.strictEqual(fmtDur(182), '3m 2s');
  assert.strictEqual(fmtDur(3840), '1h 4m');
});

test('fmtDur clamps negatives and junk to 0.0s', () => {
  assert.strictEqual(fmtDur(-5), '0.0s');
  assert.strictEqual(fmtDur('not a number'), '0.0s');
});

test('buildRebuildClosure walks transitive reverse-dependents (who recompiles)', () => {
  // A → B → C  (X depends on Y means edge X→Y). Touch C ⇒ B and A recompile.
  const edges = [{ from: 'A', to: 'B' }, { from: 'B', to: 'C' }];
  assert.deepStrictEqual(sorted(buildRebuildClosure(ds(edges), 'C')), ['A', 'B', 'C']);
  assert.deepStrictEqual(sorted(buildRebuildClosure(ds(edges), 'B')), ['A', 'B']);
  assert.deepStrictEqual(sorted(buildRebuildClosure(ds(edges), 'A')), ['A']);
});

test('buildDependencyClosure walks transitive dependencies (compile-before set)', () => {
  const edges = [{ from: 'A', to: 'B' }, { from: 'B', to: 'C' }];
  assert.deepStrictEqual(sorted(buildDependencyClosure(ds(edges), 'A')), ['A', 'B', 'C']);
  assert.deepStrictEqual(sorted(buildDependencyClosure(ds(edges), 'B')), ['B', 'C']);
  assert.deepStrictEqual(sorted(buildDependencyClosure(ds(edges), 'C')), ['C']);
});

test('closures ignore the __ext__ sentinel and null endpoints', () => {
  const edges = [
    { from: 'A', to: 'B' },
    { from: 'A', to: '__ext__' },
    { from: '__ext__', to: 'B' },
    { from: null, to: 'B' },
    { from: 'A', to: null },
  ];
  assert.deepStrictEqual(sorted(buildDependencyClosure(ds(edges), 'A')), ['A', 'B']);
  assert.deepStrictEqual(sorted(buildRebuildClosure(ds(edges), 'B')), ['A', 'B']);
});

test('closures terminate on cycles', () => {
  // A ⇄ B plus B → C. No infinite loop; closure is the whole reachable set.
  const edges = [{ from: 'A', to: 'B' }, { from: 'B', to: 'A' }, { from: 'B', to: 'C' }];
  assert.deepStrictEqual(sorted(buildDependencyClosure(ds(edges), 'A')), ['A', 'B', 'C']);
  assert.deepStrictEqual(sorted(buildRebuildClosure(ds(edges), 'C')), ['A', 'B', 'C']);
});

test('tarjanSccs: a DAG is all singletons', () => {
  const deps = { A: ['B'], B: ['C'], C: [] };
  assert.deepStrictEqual(normSccs(tarjanSccs(['A', 'B', 'C'], deps)), [['A'], ['B'], ['C']]);
});

test('tarjanSccs: a 2-cycle is one component', () => {
  // Mirrors the shared fixture topology: Core ⇄ Util, Feature → Core, App → Feature.
  const deps = {
    App: ['Feature'], Feature: ['Core'], Core: ['Util'], Util: ['Core'],
  };
  const nodes = ['App', 'Feature', 'Core', 'Util'];
  assert.deepStrictEqual(
    normSccs(tarjanSccs(nodes, deps)),
    [['App'], ['Core', 'Util'], ['Feature']],
  );
});

test('tarjanSccs: a 3-cycle collapses fully', () => {
  const deps = { A: ['B'], B: ['C'], C: ['A'] };
  assert.deepStrictEqual(normSccs(tarjanSccs(['A', 'B', 'C'], deps)), [['A', 'B', 'C']]);
});

test('tarjanSccs: isolated nodes and missing deps keys are singletons', () => {
  const deps = { A: ['B'] }; // B, C have no entry
  assert.deepStrictEqual(normSccs(tarjanSccs(['A', 'B', 'C'], deps)), [['A'], ['B'], ['C']]);
});

test('tarjanSccs: every node appears in exactly one component', () => {
  const deps = { A: ['B'], B: ['A'], C: ['D'], D: ['C'], E: ['A'] };
  const nodes = ['A', 'B', 'C', 'D', 'E'];
  const flat = tarjanSccs(nodes, deps).flat().sort();
  assert.deepStrictEqual(flat, ['A', 'B', 'C', 'D', 'E']);
});

test('migrationPlanOrder: linear chain migrates leaf-first', () => {
  // A→B→C (depends-on). C is depended on by all → highest reverse-reach → first.
  const steps = migrationPlanOrder(['A', 'B', 'C'], { A: ['B'], B: ['C'] }, []);
  assert.deepStrictEqual(planFolders(steps), [['C'], ['B'], ['A']]);
  assert.strictEqual(steps[0].step, 1);
  assert.deepStrictEqual(steps.map(s => s.is_cycle), [false, false, false]);
});

test('migrationPlanOrder: a cycle bundles into one step', () => {
  // Fixture topology: App→Feature→Core⇄Util.
  const deps = { App: ['Feature'], Feature: ['Core'], Core: ['Util'], Util: ['Core'] };
  const steps = migrationPlanOrder(['App', 'Feature', 'Core', 'Util'], deps, []);
  assert.deepStrictEqual(planFolders(steps), [['Core', 'Util'], ['Feature'], ['App']]);
  assert.deepStrictEqual(steps.map(s => s.is_cycle), [true, false, false]);
  assert.strictEqual(steps[0].size, 2);
});

test('migrationPlanOrder: a step records what it unlocks', () => {
  const steps = migrationPlanOrder(['A', 'B'], { A: ['B'] }, []);
  // Migrating B (first) unlocks A.
  assert.deepStrictEqual(steps[0].folders, ['B']);
  assert.deepStrictEqual(steps[0].unlocks, [{ folders: ['A'], size: 1 }]);
});

test('migrationPlanOrder: inbound weight breaks ties (most-used first)', () => {
  // Two independent leaves X and Y (nothing depends on either → equal reach 0),
  // consumed by an in-scope C. Y is referenced more heavily → migrates first.
  const sourceSet = ['C', 'X', 'Y'];
  const deps = { C: ['X', 'Y'] };
  const wedges = [
    { src: 'C', dst: 'X', w: 1 },
    { src: 'C', dst: 'Y', w: 9 },
  ];
  const steps = migrationPlanOrder(sourceSet, deps, wedges);
  // C depends on both, so it's last; among {X,Y} the heavier-referenced Y wins.
  assert.deepStrictEqual(planFolders(steps), [['Y'], ['X'], ['C']]);
});

test('migrationPlanOrder: deterministic regardless of input order', () => {
  const a = migrationPlanOrder(['A', 'B', 'C'], { A: ['B'], B: ['C'] }, []);
  const b = migrationPlanOrder(['C', 'A', 'B'], { B: ['C'], A: ['B'] }, []);
  assert.deepStrictEqual(planFolders(a), planFolders(b));
});

test('migrationPlanOrder: scores rank the frontier by ROI', () => {
  // X and Y tie on reach; Y's higher payoff-per-effort puts it first.
  const steps = migrationPlanOrder(['C', 'X', 'Y'], { C: ['X', 'Y'] }, [], {
    X: { payoff: 10, effort: 10 },
    Y: { payoff: 50, effort: 1 },
    C: { payoff: 0, effort: 1 },
  });
  assert.deepStrictEqual(planFolders(steps), [['Y'], ['X'], ['C']]);
  assert.strictEqual(steps[0].roi, 50);
  assert.strictEqual(steps[0].payoff, 50);
  assert.strictEqual(steps[0].effort, 1);
});

test('migrationPlanOrder: roi never violates topology', () => {
  // A scores huge but depends on B — B still goes first.
  const steps = migrationPlanOrder(['A', 'B'], { A: ['B'] }, [], {
    A: { payoff: 100, effort: 1 },
    B: { payoff: 1, effort: 1 },
  });
  assert.deepStrictEqual(planFolders(steps), [['B'], ['A']]);
});

test('migrationPlanOrder: steps carry waves (dependency depth)', () => {
  const steps = migrationPlanOrder(['A', 'B', 'C'], { C: ['A'] }, []);
  const waves = {};
  steps.forEach(s => { waves[s.folders[0]] = s.wave; });
  assert.deepStrictEqual(waves, { A: 1, B: 1, C: 2 });
});

test('migrationPlanOrder: score fields are null without scores', () => {
  const steps = migrationPlanOrder(['A'], {}, []);
  assert.strictEqual(steps[0].roi, null);
  assert.strictEqual(steps[0].payoff, null);
  assert.strictEqual(steps[0].wave, 1);
});

// ── decodePayload ────────────────────────────────────────────────────────────

test('decodePayload: expands interned sections to the original shapes', () => {
  const encoded = {
    strings: ['App', 'Core', 'A.swift', 'AppT', 'CoreT', 'App/A.swift', 'Core/C.swift'],
    edges: [[0, 1, 2]],
    files: [[0, 2, [3], [4], [[4, 1]]]],
    file_edges: [[5, 6, 1, [4]]],
    type_edges: [[3, 4, 1, [4], 5, 6]],
    plan: [{ step: 1 }],
  };
  const d = decodePayload(encoded);
  assert.deepStrictEqual(d.edges, [{ src: 'App', dst: 'Core', w: 2 }]);
  assert.deepStrictEqual(d.files, [{
    folder: 'App', name: 'A.swift', decls: ['AppT'], refs: ['CoreT'],
    ref_owners: [['CoreT', 'Core']],
  }]);
  assert.deepStrictEqual(d.file_edges, [{
    src: 'App/A.swift', dst: 'Core/C.swift', w: 1, symbols: ['CoreT'],
  }]);
  assert.deepStrictEqual(d.type_edges, [{
    src: 'AppT', dst: 'CoreT', w: 1, symbols: ['CoreT'],
    src_file: 'App/A.swift', dst_file: 'Core/C.swift',
  }]);
  assert.deepStrictEqual(d.plan, [{ step: 1 }]);   // untouched key passes through
  assert.strictEqual(d.strings, undefined);        // table consumed, not exposed
});

test('decodePayload: passes a payload without a strings table through', () => {
  const plain = { edges: [{ src: 'A', dst: 'B', w: 1 }], plan: [] };
  assert.strictEqual(decodePayload(plain), plain);
});

test('decodePayload: tolerates missing sections', () => {
  const d = decodePayload({ strings: ['x'], edges: [], files: [],
                            file_edges: [], type_edges: [] });
  assert.deepStrictEqual(d.edges, []);
  assert.deepStrictEqual(d.type_edges, []);
});

// ── resourcesUnder ───────────────────────────────────────────────────────────

test('resourcesUnder: own folder entries come back bare', () => {
  const map = { Core: ['View.xib', 'Assets.xcassets'] };
  assert.deepStrictEqual(resourcesUnder(map, 'Core'), ['View.xib', 'Assets.xcassets']);
});

test('resourcesUnder: subfolder entries are prefixed with their relative path', () => {
  const map = { 'Core/UI': ['Cell.xib'], Core: ['Top.strings'] };
  assert.deepStrictEqual(resourcesUnder(map, 'Core'), ['Top.strings', 'UI/Cell.xib']);
});

test('resourcesUnder: sibling prefixes do not leak (Core vs CoreFoo)', () => {
  const map = { CoreFoo: ['Nope.xib'], Core: ['Yes.xib'] };
  assert.deepStrictEqual(resourcesUnder(map, 'Core'), ['Yes.xib']);
});

test('resourcesUnder: empty/missing map yields empty list', () => {
  assert.deepStrictEqual(resourcesUnder({}, 'Core'), []);
  assert.deepStrictEqual(resourcesUnder(undefined, 'Core'), []);
});

// ── masterStepPrompt ──────────────────────────────────────────────────────────
const { masterStepPrompt } = require('../../modgraph/templates/graph_logic.js');

function apiImplStep(extra) {
  return Object.assign({
    id: 'qw:Feat', kind: 'new_module', phase: 1, subject: 'Feat',
    title: 'Extract Feat as a new module',
    shape: {
      mode: 'api_impl', rule: '2 consumer module(s)',
      api_module: 'FeatAPI', impl_module: 'Feat', destination: null,
      consumers: 2, api_surface_count: 2,
      api_surface: ['FeatModel', 'FeatVM'], protocols_for: ['FeatVM'],
    },
    what: { files: 3, types: 3, resources_count: 1, resources: ['Feat.xcassets'] },
    why: { narrative: 'Extractable today.', roi: 1.0, payoff: 40, effort: 10 },
    after: [], unblocks: [], details: {},
    verify: { commands: ['just refresh'], expect: { modules: '2 → 4' } },
  }, extra || {});
}

test('masterStepPrompt api_impl names both packages and the surface', () => {
  const p = masterStepPrompt(apiImplStep(), { files: ['A.swift', 'B.swift'] });
  assert.match(p, /FeatAPI/);
  assert.match(p, /`Feat`/);
  assert.match(p, /`FeatModel`/);                      // value type moves whole
  assert.match(p, /protocol in `FeatAPI` mirroring/);  // class gets a protocol
  assert.match(p, /composition root/);
  assert.match(p, /behavior-preserving/);
  assert.match(p, /Bundle\.module/);                   // resources move along
  assert.match(p, /A\.swift/);
  assert.match(p, /just refresh/);
  assert.match(p, /modules: 2 → 4/);
});

test('masterStepPrompt cut_then_extract lists the cut first', () => {
  const s = apiImplStep({ kind: 'cut_then_extract' });
  const p = masterStepPrompt(s, {
    files: [], cut: { total_refs: 6, edges: [
      { dst: 'Other', refs: 6, fix: 'invert', evidence: ['T3'] }] },
  });
  assert.match(p, /cut the 6 blocking reference/);
  assert.match(p, /fix: invert/);
  assert.ok(p.indexOf('blocking reference') < p.indexOf('API/implementation'));
});

test('masterStepPrompt move_file is a git mv instruction', () => {
  const p = masterStepPrompt({
    id: 'move:B/F.swift', kind: 'move_file', subject: 'B/F.swift',
    title: 'Move F.swift → X/',
    shape: { mode: 'move_file', rule: 'reference affinity', destination: 'X' },
    what: { files: 1 }, why: { narrative: '4 of its references bind to X.' },
    details: { src: 'B', dst: 'X' },
    verify: { commands: [], expect: {} },
  }, { move: { file: 'B/F.swift', to: 'X', symbols: ['T1'] } });
  assert.match(p, /git mv B\/F\.swift X\//);
  assert.match(p, /T1/);
});

test('masterStepPrompt join folds into the consumer', () => {
  const p = masterStepPrompt({
    id: 'join:Q', kind: 'join_module', subject: 'Q',
    title: 'Fold Q into P',
    shape: { mode: 'join', rule: 'boundary without benefit', destination: 'P' },
    what: { types: 4 }, why: { narrative: 'Only P depends on it.' },
    verify: { commands: [], expect: {} },
  }, {});
  assert.match(p, /into `P`/);
  assert.match(p, /Package\.swift/);
  assert.match(p, /dynamic loading/);                  // the escape hatch stays
});

test('masterStepPrompt single_module skips the API split', () => {
  const s = apiImplStep();
  s.shape = { mode: 'single_module', rule: 'one consumer', impl_module: 'Feat',
              api_module: null, api_surface: [], protocols_for: [],
              api_surface_count: 0, destination: null };
  const p = masterStepPrompt(s, { files: [] });
  assert.match(p, /single|No API split/i);
  assert.ok(!/composition root/.test(p));
});
