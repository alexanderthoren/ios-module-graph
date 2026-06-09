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
  escapeHtml, fmtDur, buildRebuildClosure, buildDependencyClosure,
} = require('../../modgraph/templates/graph_logic.js');

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
