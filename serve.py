#!/usr/bin/env python3
"""Companion HTTP server for `just serve`.

Two jobs, both bound to 127.0.0.1:
  1. Open files/folders in Xcode via `xed` (cmd+click in the HTML graph).
  2. Hot-reload the browser when dependency_graph.html is regenerated.

Pure stdlib. No deps. Serves static files from --root-html dir so the
browser can hit http://127.0.0.1:PORT/dependency_graph.html directly.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Hub:
    """Tracks subscribed SSE clients and broadcasts reload events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue[str]] = []

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=8)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def broadcast(self, event: str) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


def watch_html(html_path: Path, hub: Hub) -> None:
    """Poll the HTML file's mtime; broadcast 'reload' on change."""
    last = html_path.stat().st_mtime if html_path.exists() else 0.0
    while True:
        time.sleep(0.5)
        try:
            mt = html_path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mt != last:
            last = mt
            hub.broadcast("reload")


class Handler(BaseHTTPRequestHandler):
    # Set by main(): server_html_dir, server_repo_root, server_hub.
    server_html_dir: Path
    server_repo_root: Path
    server_hub: Hub

    def log_message(self, fmt: str, *args) -> None:  # quieter
        sys.stderr.write("[serve] " + fmt % args + "\n")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/ping":
            self._json(200, {"ok": True, "repo": str(self.server_repo_root)})
            return
        if self.path == "/events":
            self._serve_sse()
            return
        self._serve_static()

    def do_POST(self) -> None:
        if self.path != "/open":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        rel = (body.get("path") or "").strip()
        line = body.get("line")
        if not rel:
            self._json(400, {"error": "missing path"})
            return
        # Resolve relative paths against the repo root, then confine to it so a
        # malicious payload can't pop open arbitrary files outside the project.
        target = (self.server_repo_root / rel).resolve()
        try:
            target.relative_to(self.server_repo_root.resolve())
        except ValueError:
            self._json(403, {"error": "path escapes repo root"})
            return
        if not target.exists():
            self._json(404, {"error": "not found", "path": str(target)})
            return
        cmd = ["xed"]
        if isinstance(line, int) and line > 0:
            cmd += ["-l", str(line)]
        cmd.append(str(target))
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            self._json(500, {"error": "xed not on PATH"})
            return
        self._json(200, {"ok": True, "path": str(target)})

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = self.server_hub.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=15)
                    payload = f"event: {evt}\ndata: {int(time.time())}\n\n"
                except queue.Empty:
                    payload = ": ping\n\n"
                try:
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            self.server_hub.unsubscribe(q)

    def _serve_static(self) -> None:
        path = self.path.split("?", 1)[0].lstrip("/")
        if not path:
            path = "dependency_graph.html"
        target = (self.server_html_dir / path).resolve()
        try:
            target.relative_to(self.server_html_dir.resolve())
        except ValueError:
            self._json(403, {"error": "path escapes html dir"})
            return
        if not target.is_file():
            self._json(404, {"error": "not found"})
            return
        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript",
            ".json": "application/json",
            ".css": "text/css",
            ".md": "text/markdown; charset=utf-8",
        }.get(ext, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Strong no-cache: hot reload must always pick up the fresh HTML.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--html", required=True, help="path to dependency_graph.html")
    ap.add_argument("--root", required=True, help="absolute path of the analysed project (for xed)")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    html_path = Path(args.html).resolve()
    repo_root = Path(args.root).resolve()
    if not html_path.exists():
        print(f"✗ html not found: {html_path}", file=sys.stderr)
        return 1
    if not repo_root.is_dir():
        print(f"✗ project root not found: {repo_root}", file=sys.stderr)
        return 1

    hub = Hub()
    Handler.server_html_dir = html_path.parent
    Handler.server_repo_root = repo_root
    Handler.server_hub = hub

    watcher = threading.Thread(target=watch_html, args=(html_path, hub), daemon=True)
    watcher.start()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/{html_path.name}"
    print(f"→ serving {html_path.name} at {url}")
    print(f"→ project root: {repo_root}")
    print("→ hot reload: edits to the HTML refresh open tabs")
    print("→ cmd+click any folder in the HTML to open it in Xcode (xed)")
    print("→ Ctrl-C to stop")
    if not args.no_open:
        try:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n→ stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
