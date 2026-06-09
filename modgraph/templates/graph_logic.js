/* Pure, DOM-free graph/formatting helpers extracted from template.html so they
 * can be unit-tested under Node (tests/js/graph_logic.test.js) without a browser
 * or a DOM. This file is dual-mode:
 *   - inlined into the rendered HTML as a classic <script> by render.py, so the
 *     functions become globals the main script calls, exactly as before;
 *   - require()'d by the Node test (the module.exports guard below fires only
 *     when `module` exists, i.e. under Node — it is a no-op in the browser).
 * Keep these functions pure: arguments in, value out, no DOM, no globals. If a
 * helper needs the DOM or page state, it does NOT belong here. */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// Human-friendly duration from seconds: "0.4s", "42s", "3m 2s", "1h 4m".
function fmtDur(secs) {
  const s = Math.max(0, +secs || 0);
  if (s < 1) return s.toFixed(1) + 's';
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) { const m = Math.floor(s / 60); return m + 'm ' + Math.round(s - m * 60) + 's'; }
  const h = Math.floor(s / 3600); return h + 'h ' + Math.round((s - h * 3600) / 60) + 'm';
}
// Build mode: light up the hovered folder's *rebuild set* — every folder that
// transitively depends on it (and so recompiles when it changes), with the
// propagation edges drawn red. Reverse-BFS over the displayed dependency edges
// (edge from→to means from depends on to, so rebuilds flow to→from).
function buildRebuildClosure(edgesDS, focusId) {
  const radj = new Map();
  edgesDS.get().forEach(e => {
    if (e.from == null || e.to == null) return;
    if (e.from === '__ext__' || e.to === '__ext__') return;
    if (!radj.has(e.to)) radj.set(e.to, []);
    radj.get(e.to).push(e.from);
  });
  const closure = new Set([focusId]);
  const stack = [focusId];
  while (stack.length) {
    const x = stack.pop();
    (radj.get(x) || []).forEach(a => { if (!closure.has(a)) { closure.add(a); stack.push(a); } });
  }
  return closure;
}
// Build mode (cold lens): light up the hovered module's *dependency set* — every
// module it transitively depends on, which must compile *before* it on a clean
// build. Forward-BFS over the displayed edges (edge from→to means from depends on
// to, so we follow from→to).
function buildDependencyClosure(edgesDS, focusId) {
  const adj = new Map();
  edgesDS.get().forEach(e => {
    if (e.from == null || e.to == null) return;
    if (e.from === '__ext__' || e.to === '__ext__') return;
    if (!adj.has(e.from)) adj.set(e.from, []);
    adj.get(e.from).push(e.to);
  });
  const closure = new Set([focusId]);
  const stack = [focusId];
  while (stack.length) {
    const x = stack.pop();
    (adj.get(x) || []).forEach(b => { if (!closure.has(b)) { closure.add(b); stack.push(b); } });
  }
  return closure;
}

// Iterative Tarjan's SCC over an explicit graph — the algorithmic core shared
// by the migration wizard (wizComputeSccs builds `deps` from app state, then
// delegates here). Iterative to dodge deep recursion on big graphs. This mirrors
// Python's modgraph.graph._tarjan_sccs (same algorithm, cross-checked in tests)
// so the in-browser plan matches the CLI plan.
//   nodes: iterable of node ids.
//   deps:  { node: [neighbour, ...] } adjacency (a node may be absent → no out-edges).
// Returns an array of SCCs, each an array of node ids.
function tarjanSccs(nodes, deps) {
  const idx = {}, low = {}, onS = {}, st = [], sccs = [];
  let counter = 0;
  for (const start of nodes) {
    if (idx[start] !== undefined) continue;
    const work = [[start, 0]];
    idx[start] = counter; low[start] = counter; counter++;
    st.push(start); onS[start] = true;
    while (work.length) {
      const top = work[work.length - 1];
      const v = top[0];
      const out = deps[v] || [];
      if (top[1] < out.length) {
        const w = out[top[1]++];
        if (idx[w] === undefined) {
          idx[w] = counter; low[w] = counter; counter++;
          st.push(w); onS[w] = true;
          work.push([w, 0]);
        } else if (onS[w]) {
          low[v] = Math.min(low[v], idx[w]);
        }
      } else {
        if (low[v] === idx[v]) {
          const comp = [];
          while (true) {
            const w = st.pop(); onS[w] = false; comp.push(w);
            if (w === v) break;
          }
          sccs.push(comp);
        }
        work.pop();
        if (work.length) { const p = work[work.length - 1][0]; low[p] = Math.min(low[p], low[v]); }
      }
    }
  }
  return sccs;
}

// Node-only: expose the helpers to the test runner. Guarded so the browser
// (where `module` is undefined) skips it without error.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    escapeHtml, fmtDur, buildRebuildClosure, buildDependencyClosure, tarjanSccs,
  };
}
