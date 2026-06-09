# Vendored assets

These third-party files are committed to the repo (not fetched at runtime) so a
generated graph is a self-contained single file that works offline / from
`file://`. `render.py` inlines them into the output HTML.

## `vis-network.min.js`

- **Library:** [vis-network](https://github.com/visjs/vis-network) — the graph
  rendering engine.
- **Version:** `9.1.9`
- **Source:** https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js
- **License:** MIT / Apache-2.0 (dual), © vis.js contributors.
- **sha256:** `f53f833ddb9bf97efe856bb0637d4fe88f39e39999c7e94a4b8afc8de8a1a2e5`

`tests/test_vendor_integrity.py` re-hashes the file on every test run and
asserts it matches the sha256 above — so an accidental edit or a botched update
fails loudly instead of silently shipping a corrupted bundle.

### Updating

```sh
VER=9.1.9   # set to the new version
curl -sSL -o vis-network.min.js \
  "https://unpkg.com/vis-network@${VER}/standalone/umd/vis-network.min.js"
shasum -a 256 vis-network.min.js   # paste the digest + version above
```

Then run the suite. Before trusting a new bundle, confirm it contains no
`</script` sequence (it would break inlining — `render.py` inlines it verbatim);
`tests/test_render.py::RenderHtmlSelfContainedTest` already guards this.
