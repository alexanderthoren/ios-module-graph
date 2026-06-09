"""Integrity check for vendored third-party assets.

The vis-network bundle is committed and inlined verbatim into every generated
graph, so an accidental edit (or a botched update) would silently ship a
corrupted/altered engine. Pin its sha256 to the value recorded in the vendor
README and re-verify on every test run; mismatch fails loudly.

When intentionally updating the bundle, update the digest in
modgraph/templates/vendor/README.md (see its "Updating" section) and this test
will pass again — the README is the single source of truth for the expected
hash, parsed here so the two can't drift.
"""
from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "modgraph" / "templates" / "vendor"
_BUNDLE = _VENDOR / "vis-network.min.js"
_README = _VENDOR / "README.md"


def _expected_sha256() -> str:
    """Pull the recorded sha256 out of the vendor README (single source of truth)."""
    text = _README.read_text(encoding="utf-8")
    m = re.search(r"\*\*sha256:\*\*\s*`([0-9a-f]{64})`", text)
    if not m:
        raise AssertionError("no `**sha256:** `<hex>`` line found in vendor/README.md")
    return m.group(1)


class VendorIntegrityTest(unittest.TestCase):
    def test_bundle_exists(self):
        self.assertTrue(_BUNDLE.is_file(), f"missing vendored bundle: {_BUNDLE}")

    def test_bundle_matches_recorded_sha256(self):
        actual = hashlib.sha256(_BUNDLE.read_bytes()).hexdigest()
        self.assertEqual(
            actual, _expected_sha256(),
            "vis-network.min.js does not match the sha256 in vendor/README.md — "
            "if this was an intentional update, refresh the digest there.",
        )

    def test_bundle_has_no_script_breakout(self):
        # It is inlined verbatim into a <script> block; a </script in it would
        # close the element early.
        self.assertNotIn("</script", _BUNDLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
