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
//   scores:    optional { folder: {payoff, effort} } — payoff already resolved
//              by the caller to churn-weighted hot or structural combined
//              (e.g. from the quick_wins payload). When present, the eligible
//              frontier is ranked by per-SCC ROI first, mirroring Python's
//              compute_migration_plan(scores=...). Rounding mirrors Python
//              (payoff to 1 decimal, roi to 2) so the orders agree.
//
// Returns ordered steps: { step, folders (sorted), is_cycle, size, unlocks,
// inbound_weight, wave, payoff, effort, roi }. Folders that cyclically depend
// bundle into one step; `wave` = 1 + longest dependency chain beneath the SCC
// (same wave ⇒ no deps between steps ⇒ parallelizable), matching Python.
//
// NOTE: this matches Python on the primary signals (ROI when scores are given,
// then transitive reverse-reach) and the SCC bundling, but the wizard adds
// `inbound_weight` ("most-used") as a tiebreaker that Python does not have — so
// the two agree on step ORDER only when ROI/reverse-reach alone determine it
// (and always agree on the set of steps).
function migrationPlanOrder(sourceSet, deps, weightedEdges, scores) {
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
  const wave = sccs.map(() => 1);

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
    // Wave = 1 + longest dependency chain beneath an SCC. topo lists
    // dependencies before dependents, so one forward pass suffices.
    for (const v of topo) {
      let deepest = 0;
      for (const d of sdeps[v]) deepest = Math.max(deepest, wave[d]);
      wave[v] = 1 + deepest;
    }
  }

  // Per-SCC ROI from the folder scores (mirrors Python: payoff and effort sum
  // over members; payoff rounded to 1 decimal, roi to 2).
  const sccPayoff = sccs.map(() => 0);
  const sccEffort = sccs.map(() => 0);
  const sccRoi = sccs.map(() => 0);
  if (scores) {
    sccs.forEach((c, i) => {
      let p = 0, e = 0;
      for (const f of c) {
        const row = scores[f] || {};
        p += row.payoff || 0;
        e += row.effort || 0;
      }
      sccPayoff[i] = Math.round(p * 10) / 10;
      sccEffort[i] = e;
      sccRoi[i] = Math.round((p / Math.max(e, 1)) * 100) / 100;
    });
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
      // Numeric ranks (higher wins) in priority order; the SCC's first folder
      // name is the final ascending tie-break. ROI leads only when scores are
      // present, exactly like Python.
      const key = [
        ...(scores ? [sccRoi[i]] : []),
        reverseReach[i], inboundWeight[i], immediateUnlocks(i),
        -sccs[i].length,
      ];
      const name = sccs[i][0] || '';
      let better = best === null;
      if (!better) {
        let decided = false;
        for (let k = 0; k < key.length; k++) {
          if (key[k] !== best.key[k]) { better = key[k] > best.key[k]; decided = true; break; }
        }
        if (!decided) better = name < best.name;
      }
      if (better) { best = { key, name }; pick = i; }
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
      wave: wave[pick],
      payoff: scores ? sccPayoff[pick] : null,
      effort: scores ? sccEffort[pick] : null,
      roi: scores ? sccRoi[pick] : null,
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

// ── resources per migration step ─────────────────────────────────────────────
// Aggregate the bundle resources a folder move drags along: entries of the
// folder itself plus everything under it (a move takes the whole subtree).
// `map` is payload.resources ({folder id -> [names]}); entries from subfolders
// come back prefixed with their path relative to `folder` so the prompt shows
// where each file lives. Deterministic: keys walked sorted.
function resourcesUnder(map, folder) {
  const out = [];
  Object.keys(map || {}).sort().forEach(k => {
    if (k === folder) {
      map[k].forEach(n => out.push(n));
    } else if (k.startsWith(folder + '/')) {
      const rel = k.slice(folder.length + 1);
      map[k].forEach(n => out.push(rel + '/' + n));
    }
  });
  return out;
}

// ── master-plan step prompt ──────────────────────────────────────────────────
// One self-contained, executable instruction per master-plan step (the 📝
// button on a Plan card). Pure: everything it says comes from the step record
// plus an explicit ctx — no DOM, no payload globals — so it is unit-tested
// under Node. ctx (all optional):
//   files: [names]      — source files of a folder subject
//   move:  file_moves item (symbols evidence) for move_file steps
//   cut:   quick-win cut {edges:[{dst,refs,fix,evidence}],total_refs} for
//          cut_then_extract steps
function masterStepPrompt(step, ctx) {
  ctx = ctx || {};
  const sh = step.shape || {};
  const what = step.what || {};
  const why = step.why || {};
  const verify = step.verify || {};
  const L = [];
  const guard = 'This change must be **behavior-preserving**: move code, adjust '
    + 'access levels, rewire imports, introduce protocols — never edit logic. '
    + 'Keep it one reviewable PR. The dependency graph cannot judge domain '
    + 'cohesion or naming — if this move is semantically wrong, say so and '
    + 'stop instead of executing it.';
  const resources = (what.resources || []);
  const resLine = what.resources_count
    ? 'Move the ' + what.resources_count + ' bundle resource(s) along ('
      + resources.join(', ') + (what.resources_count > resources.length ? ', …' : '')
      + '), declare them in Package.swift, and switch their Bundle.main '
      + 'lookups to Bundle.module.'
    : null;
  const pushVerify = () => {
    L.push('');
    L.push('## Verify');
    (verify.commands || []).forEach(c => L.push('- `' + c + '`'));
    const exp = verify.expect || {};
    const keys = Object.keys(exp);
    if (keys.length) {
      L.push('');
      L.push('Expected movement:');
      keys.forEach(k => L.push('- ' + k + ': ' + exp[k]));
    }
  };
  const pushFiles = () => {
    if ((ctx.files || []).length) {
      L.push('');
      L.push('## Files to move');
      ctx.files.forEach(f => L.push('- `' + f + '`'));
    }
  };
  const pushCut = () => {
    if (step.kind === 'cut_then_extract' && ctx.cut && (ctx.cut.edges || []).length) {
      L.push('');
      L.push('## First: cut the ' + ctx.cut.total_refs + ' blocking reference(s)');
      ctx.cut.edges.forEach(e => {
        L.push('- → `' + e.dst + '` (' + e.refs + ' ref(s)) — fix: ' + e.fix
          + ((e.evidence || []).length ? ' (' + e.evidence.join(', ') + ')' : ''));
      });
      L.push('Fix meanings: move_file = the file belongs in the target folder; '
        + 'shared_primitive = push the type down into a shared foundation '
        + 'package; invert = own a protocol (it belongs in the API package '
        + 'this step creates) and let the target conform.');
    }
  };

  L.push('# ' + step.title);
  L.push('');
  if (why.narrative) L.push(why.narrative);
  if (sh.rule) L.push('Shape decision: ' + sh.rule + '.');

  if (sh.mode === 'move_file') {
    const m = ctx.move || {};
    L.push('');
    L.push('## Steps');
    L.push('1. `git mv ' + (m.file || step.subject) + ' ' + (sh.destination || m.to)
      + '/` (keep history).');
    L.push('2. Update imports/target membership the move requires — nothing else.'
      + ((m.symbols || []).length ? ' Evidence: its references to '
         + m.symbols.join(', ') + ' bind there.' : ''));
  } else if (sh.mode === 'absorb') {
    pushCut();
    pushFiles();
    L.push('');
    L.push('## Steps');
    L.push('1. Move the folder\'s ' + (what.files || 0) + ' file(s) into the existing '
      + 'module `' + sh.destination + '` (its own target).');
    if (resLine) L.push('2. ' + resLine);
    L.push((resLine ? '3' : '2') + '. Mark only the types the compiler demands '
      + 'as public — no speculative API.');
    L.push((resLine ? '4' : '3') + '. Point the app target (and any consumer) at '
      + '`' + sh.destination + '` where it previously found these types.');
  } else if (sh.mode === 'api_impl') {
    pushCut();
    pushFiles();
    const surface = sh.api_surface || [];
    const protocols = sh.protocols_for || [];
    const values = surface.filter(t => protocols.indexOf(t) < 0);
    L.push('');
    L.push('## Steps — ship as an API/implementation pair');
    L.push('1. Create the library target `' + sh.api_module + '`: it holds the '
      + 'cross-module surface and depends on nothing first-party (other API '
      + 'packages at most).');
    if (values.length) {
      L.push('   - Move these value types/protocols into `' + sh.api_module + '` whole: '
        + values.map(t => '`' + t + '`').join(', ')
        + (sh.api_surface_count > surface.length ? ', …(' + sh.api_surface_count + ' total)' : '') + '.');
    }
    if (protocols.length) {
      L.push('   - For each of ' + protocols.map(t => '`' + t + '`').join(', ')
        + ' (reference types with behavior): introduce a protocol in `'
        + sh.api_module + '` mirroring its externally-used members.');
    }
    L.push('2. Create the implementation target `' + sh.impl_module + '` with the '
      + 'remaining ' + (what.files || 0) + ' file(s); it depends on `'
      + sh.api_module + '` plus other modules\' API packages — never their '
      + 'implementations. Conform each class to its new protocol.');
    if (resLine) L.push('3. ' + resLine);
    L.push((resLine ? '4' : '3') + '. Rewire every consumer to import `'
      + sh.api_module + '` only' + (sh.consumers ? ' (' + sh.consumers
      + ' consumer module(s), current or future)' : '') + '.');
    L.push((resLine ? '5' : '4') + '. Bind at the composition root: the app\'s '
      + 'entry point instantiates `' + sh.impl_module + '`\'s classes and hands '
      + 'them out as `' + sh.api_module + '` protocols. Only the composition '
      + 'root imports `' + sh.impl_module + '`.');
  } else if (sh.mode === 'single_module') {
    pushCut();
    pushFiles();
    L.push('');
    L.push('## Steps');
    L.push('1. Create the library target `' + sh.impl_module + '` and move the '
      + 'folder\'s ' + (what.files || 0) + ' file(s) into it.');
    if (resLine) L.push('2. ' + resLine);
    L.push((resLine ? '3' : '2') + '. Mark only the types the compiler demands '
      + 'as public — no speculative API.');
    L.push((resLine ? '4' : '3') + '. Add the dependency where the code was '
      + 'consumed and rewire imports. (No API split: ' + (sh.rule || 'one '
      + 'consumer') + ' — create the pair later only if consumers multiply.)');
  } else if (sh.mode === 'isolate') {
    L.push('');
    L.push('## Steps');
    L.push('1. Create the module `' + sh.impl_module + '` and move the type plus '
      + 'its drag closure (' + (what.types || 0) + ' type(s) total — the Isolate '
      + 'view lists every member).');
    L.push('2. Flip the ' + (sh.api_surface_count || 0) + ' externally-referenced '
      + 'type(s) to public.');
    L.push('3. Rewire the ' + (sh.consumers || 0) + ' external module(s) to '
      + 'depend on `' + sh.impl_module + '` instead of `' + step.subject + '`.');
  } else if (sh.mode === 'split') {
    L.push('');
    L.push('## Steps');
    L.push('1. Open the Divide view for `' + step.subject + '` — it has the '
      + 'per-unit split plan (units, order, public surface).');
    L.push('2. One new target per unit, dependency-ordered; flip only the types '
      + 'the remaining units reference to public.');
    L.push('3. Retarget consumers that only touch a split-off unit to the new '
      + 'smaller module.');
  } else if (sh.mode === 'join') {
    L.push('');
    L.push('## Steps');
    L.push('1. Move `' + step.subject + '`\'s sources into `' + sh.destination
      + '`\'s target and merge the Package.swift entries.');
    L.push('2. Drop `public` where nothing external needs it anymore.');
    L.push('3. If the module exists for a reason the dependency graph cannot '
      + 'see (dynamic loading, app extensions, a license boundary), say so '
      + 'instead of folding it.');
  }

  L.push('');
  L.push(guard);
  pushVerify();
  return L.join('\n');
}

// Node-only: expose the helpers to the test runner. Guarded so the browser
// (where `module` is undefined) skips it without error.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    escapeHtml, fmtDur, buildRebuildClosure, buildDependencyClosure, tarjanSccs,
    migrationPlanOrder, decodePayload, resourcesUnder, masterStepPrompt,
  };
}
