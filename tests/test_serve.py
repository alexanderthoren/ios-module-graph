"""Tests for modgraph.serve's pure, socketless seams: the SSE Hub pub/sub, the
/open path-confinement (the security boundary for the xed bridge), the xed
argv builder, and the SSE frame formatter. The HTTP wiring itself (BaseHTTP
handler, threads) is not exercised — only the logic that was factored out of it.
"""
from __future__ import annotations

import queue
import tempfile
import unittest
from pathlib import Path

from modgraph import serve


class HubTest(unittest.TestCase):
    def test_subscribe_returns_queue_and_broadcast_delivers(self):
        hub = serve.Hub()
        q = hub.subscribe()
        hub.broadcast("reload")
        self.assertEqual(q.get_nowait(), "reload")

    def test_broadcast_reaches_all_subscribers(self):
        hub = serve.Hub()
        qs = [hub.subscribe() for _ in range(3)]
        hub.broadcast("reload")
        for q in qs:
            self.assertEqual(q.get_nowait(), "reload")

    def test_unsubscribed_queue_gets_nothing(self):
        hub = serve.Hub()
        q = hub.subscribe()
        hub.unsubscribe(q)
        hub.broadcast("reload")
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_unsubscribe_unknown_queue_is_noop(self):
        hub = serve.Hub()
        hub.unsubscribe(queue.Queue())  # never subscribed → must not raise

    def test_broadcast_drops_silently_when_subscriber_full(self):
        hub = serve.Hub()
        q = hub.subscribe()  # maxsize=8
        for _ in range(20):
            hub.broadcast("reload")  # must not raise once the queue fills
        self.assertEqual(q.qsize(), 8)


class ResolveOpenTargetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "App").mkdir()
        (self.root / "App" / "View.swift").write_text("x", encoding="utf-8")

    def test_resolves_existing_relative_path(self):
        t = serve.resolve_open_target(self.root, "App/View.swift")
        self.assertEqual(t, self.root / "App" / "View.swift")

    def test_resolves_a_directory(self):
        self.assertEqual(serve.resolve_open_target(self.root, "App"), self.root / "App")

    def test_empty_path_is_400(self):
        for bad in ("", "   ", None):
            with self.assertRaises(serve.OpenRequestError) as cm:
                serve.resolve_open_target(self.root, bad)
            self.assertEqual(cm.exception.code, 400)

    def test_escaping_path_is_403(self):
        with self.assertRaises(serve.OpenRequestError) as cm:
            serve.resolve_open_target(self.root, "../../etc/passwd")
        self.assertEqual(cm.exception.code, 403)

    def test_absolute_path_outside_root_is_403(self):
        with self.assertRaises(serve.OpenRequestError) as cm:
            serve.resolve_open_target(self.root, "/etc/hosts")
        self.assertEqual(cm.exception.code, 403)

    def test_nonexistent_inside_root_is_404_with_path(self):
        with self.assertRaises(serve.OpenRequestError) as cm:
            serve.resolve_open_target(self.root, "App/Missing.swift")
        self.assertEqual(cm.exception.code, 404)
        self.assertIn("path", cm.exception.payload)


class BuildXedCommandTest(unittest.TestCase):
    def test_no_line(self):
        self.assertEqual(serve.build_xed_command(Path("/p/f.swift"), None),
                         ["xed", "/p/f.swift"])

    def test_positive_line_adds_flag(self):
        self.assertEqual(serve.build_xed_command(Path("/p/f.swift"), 42),
                         ["xed", "-l", "42", "/p/f.swift"])

    def test_zero_and_negative_lines_ignored(self):
        self.assertEqual(serve.build_xed_command(Path("/p/f.swift"), 0),
                         ["xed", "/p/f.swift"])
        self.assertEqual(serve.build_xed_command(Path("/p/f.swift"), -3),
                         ["xed", "/p/f.swift"])

    def test_bool_line_not_treated_as_int(self):
        # JSON `true` must not become `-l 1`.
        self.assertEqual(serve.build_xed_command(Path("/p/f.swift"), True),
                         ["xed", "/p/f.swift"])


class FormatSseEventTest(unittest.TestCase):
    def test_frame_shape(self):
        self.assertEqual(serve.format_sse_event("reload", 1700),
                         "event: reload\ndata: 1700\n\n")

    def test_ends_with_blank_line(self):
        self.assertTrue(serve.format_sse_event("ping", 1).endswith("\n\n"))


if __name__ == "__main__":
    unittest.main()
