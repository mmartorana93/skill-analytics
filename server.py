"""
server.py — Local web app + JSON API for the Skill Analytics dashboard.

Run:  python3 ~/.claude/skill-analytics/server.py [port]
Then open http://127.0.0.1:8787

Endpoints:
  GET /                         -> index.html
  GET /api/data?project=<cwd|all> -> aggregates + coverage matrix
  GET /api/log?project=&domain=&limit= -> recent Skill/MCP invocations w/ context
  GET /api/heartbeat            -> latest mtime across transcripts + live-signal
"""

import os
import sys
import json
import glob
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parser as tx_parser  # noqa: E402
import catalog  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")
LIVE_SIGNAL = os.path.join(HERE, "live-signal")
PROJECTS_DIR = tx_parser.PROJECTS_DIR

DEFAULT_PORT = 8787


def _latest_mtime():
    latest = 0.0
    for path in glob.glob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True):
        try:
            m = os.path.getmtime(path)
            if m > latest:
                latest = m
        except OSError:
            pass
    try:
        m = os.path.getmtime(LIVE_SIGNAL)
        if m > latest:
            latest = m
    except OSError:
        pass
    return latest


def build_data(project, domain="all"):
    agg = tx_parser.aggregate(project=project, domain=domain)
    cov = catalog.coverage(agg["used_skills"], project_cwd=project, domain=domain)
    return {
        "project": project or "all",
        "domain": domain,
        "projects": tx_parser.list_projects(domain=domain),
        "totals": agg["totals"],
        "skills": agg["skills"],
        "mcp": agg["mcp"],
        "servers": agg["servers"],
        "per_day": agg["per_day"],
        "coverage": cov,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send_file(INDEX_HTML, "text/html; charset=utf-8")
            return

        if route == "/api/heartbeat":
            self._send_json({"mtime": _latest_mtime()})
            return

        if route == "/api/data":
            project = (qs.get("project") or ["all"])[0]
            domain = (qs.get("domain") or ["all"])[0]
            if domain not in ("sf", "general", "all"):
                domain = "all"
            try:
                self._send_json(build_data(project, domain))
            except Exception as e:  # keep the server alive on any parse hiccup
                self._send_json({"error": str(e)}, code=500)
            return

        if route == "/api/log":
            project = (qs.get("project") or ["all"])[0]
            domain = (qs.get("domain") or ["all"])[0]
            if domain not in ("sf", "general", "all"):
                domain = "all"
            try:
                limit = int((qs.get("limit") or ["60"])[0])
            except ValueError:
                limit = 60
            limit = max(1, min(limit, 300))
            try:
                self._send_json({"events": tx_parser.recent_events(
                    project=project, domain=domain, limit=limit)})
            except Exception as e:  # keep the server alive on any parse hiccup
                self._send_json({"error": str(e)}, code=500)
            return

        self.send_error(404, "Not found")


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Skill Analytics dashboard -> {url}")
    print("Ctrl-C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
