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

// SCC-aware, deterministic migration-plan ordering — the algorithmic core of
// computeWizardPlan, extracted so it can be unit-tested and cross-checked
// against Python's modgraph.graph.compute_migration_plan.
//
//   sourceSet: array of in-scope folder ids (excluded/migrated/blocked already
//              removed by the caller).
//   deps:      { a: [b, ...] } folder→folder adjacency among sourceSet.
//   weightedEdges: [{src, dst, w}] used only for the "most-used" tiebreaker
//              (total in-scope inbound reference weight per SCC).
//
// Returns ordered steps: { step, folders (sorted), is_cycle, size, unlocks,
// inbound_weight }. Folders that cyclically depend bundle into one step.
//
// NOTE: this matches Python on the primary signal (transitive reverse-reach) and
// the SCC bundling, but the wizard adds `inbound_weight` ("most-used") as a
// secondary key that Python does not have — so the two agree on step ORDER only
// when reverse-reach alone determines it (and always agree on the set of steps).
function migrationPlanOrder(sourceSet, deps, weightedEdges) {
  const sccs = tarjanSccs(sourceSet, deps).map(c => c.slice().sort());
  const sccOf = {};
  sccs.forEach((c, i) => c.forEach(v => { sccOf[v] = i; }));

  const sdeps = sccs.map(() => new Set());
  const sRdeps = sccs.map(() => new Set());
  Object.keys(deps).forEach(a => {
    for (const b of deps[a]) {
      const sa = sccOf[a], sb = sccOf[b];
      if (sa !== sb) { sdeps[sa].add(sb); sRdeps[sb].add(sa); }
    }
  });

  const remaining = sdeps.map(s => s.size);
  const migratedScc = new Set();
  const eligible = new Set();
  for (let i = 0; i < sccs.length; i++) if (remaining[i] === 0) eligible.add(i);

  // Transitive reverse-reach via reverse-topological DP (same direct + 1-hop
  // approximation as Python — double-counts in diamonds, order stays stable).
  const reverseReach = sccs.map(() => 0);
  {
    const indeg = sdeps.map(s => s.size);
    const topo = [];
    const q = [];
    for (let i = 0; i < sccs.length; i++) if (indeg[i] === 0) q.push(i);
    while (q.length) {
      const v = q.pop();
      topo.push(v);
      for (const w of sRdeps[v]) { indeg[w]--; if (indeg[w] === 0) q.push(w); }
    }
    for (let k = topo.length - 1; k >= 0; k--) {
      const v = topo[k];
      let acc = 0;
      for (const w of sRdeps[v]) acc += 1 + reverseReach[w];
      reverseReach[v] = acc;
    }
  }

  const immediateUnlocks = i => {
    let n = 0;
    for (const s of sRdeps[i]) {
      if (migratedScc.has(s)) continue;
      if (s !== i && remaining[s] === 1) n++;
    }
    return n;
  };

  const sourceSetLookup = new Set(sourceSet);
  const inboundWeight = sccs.map(() => 0);
  (weightedEdges || []).forEach(e => {
    const dstScc = sccOf[e.dst];
    if (dstScc === undefined) return;
    if (!sourceSetLookup.has(e.src)) return;
    const srcScc = sccOf[e.src];
    if (srcScc === dstScc) return;
    inboundWeight[dstScc] += (e.w || 1);
  });

  const out = [];
  while (eligible.size) {
    let pick = -1, best = null;
    for (const i of eligible) {
      const key = [
        reverseReach[i], inboundWeight[i], immediateUnlocks(i),
        -sccs[i].length, sccs[i][0] || '',
      ];
      const better = best === null
        || key[0] > best[0]
        || (key[0] === best[0] && key[1] > best[1])
        || (key[0] === best[0] && key[1] === best[1] && key[2] > best[2])
        || (key[0] === best[0] && key[1] === best[1] && key[2] === best[2] && key[3] > best[3])
        || (key[0] === best[0] && key[1] === best[1] && key[2] === best[2] && key[3] === best[3] && key[4] < best[4]);
      if (better) { best = key; pick = i; }
    }
    eligible.delete(pick);
    migratedScc.add(pick);
    const fs = sccs[pick];
    const unlocks = [];
    for (const r of sRdeps[pick]) {
      if (migratedScc.has(r)) continue;
      remaining[r]--;
      if (remaining[r] === 0) {
        eligible.add(r);
        unlocks.push({ folders: sccs[r], size: sccs[r].length });
      }
    }
    out.push({
      step: out.length + 1, folders: fs, is_cycle: fs.length > 1,
      size: fs.length, unlocks, inbound_weight: inboundWeight[pick],
    });
  }
  return out;
}

// ── payload decoding ─────────────────────────────────────────────────────────
// The renderer string-interns the payload's heaviest sections (edges, files,
// file_edges, type_edges): every name/path lives once in a `strings` table and
// the records carry integer indices into it (see render.py `_intern_payload`).
// This expands them back to the exact object shapes the rest of the UI was
// written against, so only this one seam knows about the encoding. A payload
// without a `strings` table (older renderer) passes through untouched.
function decodePayload(data) {
  if (!data || !data.strings) return data;
  const S = data.strings;
  const out = Object.assign({}, data);
  delete out.strings;
  out.edges = (data.edges || []).map(e => ({ src: S[e[0]], dst: S[e[1]], w: e[2] }));
  out.files = (data.files || []).map(f => ({
    folder: S[f[0]], name: S[f[1]],
    decls: f[2].map(i => S[i]),
    refs: f[3].map(i => S[i]),
    ref_owners: f[4].map(pair => pair.map(i => S[i])),
  }));
  out.file_edges = (data.file_edges || []).map(e => ({
    src: S[e[0]], dst: S[e[1]], w: e[2], symbols: e[3].map(i => S[i]),
  }));
  out.type_edges = (data.type_edges || []).map(e => ({
    src: S[e[0]], dst: S[e[1]], w: e[2], symbols: e[3].map(i => S[i]),
    src_file: S[e[4]], dst_file: S[e[5]],
  }));
  return out;
}

// Node-only: expose the helpers to the test runner. Guarded so the browser
// (where `module` is undefined) skips it without error.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    escapeHtml, fmtDur, buildRebuildClosure, buildDependencyClosure, tarjanSccs,
    migrationPlanOrder, decodePayload,
  };
}
