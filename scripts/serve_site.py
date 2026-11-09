#!/usr/bin/env python3
"""Serve a published site folder (`manage.py publish --out DIR`) the way the hosted site serves it, for measuring.

The hosted site is S3 behind CloudFront (#213). This standard-library server copies what the browser sees there,
so Lighthouse (.github/workflows/web-perf.yml) measures the pages as visitors get them:

- clean URLs: a folder is its index.html (the CloudFront function); a missing path is 404.html with status 404;
- the Cache-Control the publisher gives each object (web/samples/publish.py: hashed assets immutable for a year,
  pages a minute, data five minutes);
- gzip for text, as CloudFront compresses;
- the strict CSP of the CloudFront response-headers policy, built from the folder's csp-hashes.json: no
  'unsafe-inline', so an inline block without a hash, or a style="" attribute, is blocked and logged
  in the console, where Lighthouse's errors-in-console audit sees it;
- with --api-prefix, a stub of the web API's GET <prefix>/status/<id> on the same origin: a run that ended long ago,
  so a page that polls has an answer and never reloads. The upload calls aren't stubbed (no upload happens here).

    python scripts/serve_site.py SITE_DIR [--port 8001] [--api-prefix /api] [--connect https://bucket...]
"""

import argparse
import gzip
import http.server
import json
import os
import re
import sys
from urllib.parse import unquote, urlsplit

# The same values as web/samples/publish.py (tests/test_serve_site.py checks that they agree).
IMMUTABLE = "public, max-age=31536000, immutable"
HTML_CACHE = "public, max-age=60"
DATA_CACHE = "public, max-age=300"
HASHED_NAME = re.compile(r"\.[0-9a-f]{12}\.[A-Za-z0-9]+$")

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".gz": "application/gzip",
    ".tbi": "application/octet-stream",
}
COMPRESSED_TYPES = ("text/", "application/json", "image/svg+xml")
STUB_RUN = {"status": "SUCCEEDED", "started": "2026-01-01T00:00:00+00:00", "stopped": "2026-01-01T00:10:00+00:00"}


def content_security_policy(hashes: dict, connect=()) -> str:
    """The hosted site's CSP (#213's CloudFront response headers): inline blocks only by their sha256."""

    def sources(kind):
        return " ".join(["'self'", *(f"'{h}'" for h in hashes.get(kind, []))])

    return "; ".join(
        [
            "default-src 'none'",
            "script-src " + sources("script"),
            "style-src " + sources("style"),
            "img-src 'self' data:",
            "connect-src " + " ".join(["'self'", *connect]),
            "worker-src 'self'",
            "manifest-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )


def cache_control(path: str) -> str:
    """The Cache-Control publish.py stores with the object at this path."""
    if HASHED_NAME.search(path):
        return IMMUTABLE
    return HTML_CACHE if path.endswith(".html") else DATA_CACHE


def content_type(path: str) -> str:
    return TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def resolve(root: str, url_path: str):
    """The file for a request path, or None. Folders are their index.html; nothing outside root is served."""
    rel = unquote(urlsplit(url_path).path).lstrip("/")
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, *[p for p in rel.split("/") if p]))
    if full != root and not full.startswith(root + os.sep):
        return None
    if os.path.isdir(full):
        full = os.path.join(full, "index.html")
    return full if os.path.isfile(full) else None


def make_handler(root: str, csp: str, api_prefix: str = ""):
    api_prefix = api_prefix.rstrip("/")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str, cache: str, head: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype.startswith(COMPRESSED_TYPES) and "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, 9)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _get(self, head: bool):
            path = urlsplit(self.path).path
            if api_prefix and path.startswith(api_prefix + "/status/"):
                body = json.dumps({"sample_id": unquote(path.rsplit("/", 1)[-1]), **STUB_RUN}).encode("utf-8")
                return self._send(200, body, "application/json", "no-store", head)
            full = resolve(root, path)
            status = 200
            if full is None:
                status, full = 404, os.path.join(root, "404.html")
            with open(full, "rb") as fh:
                body = fh.read()
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            self._send(status, body, content_type(full), cache_control(rel), head)

        def do_GET(self):
            self._get(head=False)

        def do_HEAD(self):
            self._get(head=True)

    return Handler


class Server(http.server.ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port already in use, and the two then share its
    # requests. Better to fail at start.
    allow_reuse_address = sys.platform != "win32"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("site", help="the folder `manage.py publish --out` wrote")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument(
        "--api-prefix",
        default="",
        help="answer GET <prefix>/status/<id> (publish with --api-base http://HOST:PORT<prefix>)",
    )
    p.add_argument(
        "--connect",
        action="append",
        default=[],
        help="extra connect-src origins (the API's, the data lake's), as on the hosted site; repeatable",
    )
    a = p.parse_args(argv)
    hashes_path = os.path.join(a.site, "csp-hashes.json")
    if not os.path.isfile(os.path.join(a.site, "404.html")) or not os.path.isfile(hashes_path):
        print(f"{a.site} isn't a published site (no 404.html or csp-hashes.json)", file=sys.stderr)
        return 1
    with open(hashes_path, encoding="utf-8") as fh:
        csp = content_security_policy(json.load(fh), a.connect)
    server = Server((a.host, a.port), make_handler(a.site, csp, a.api_prefix))
    print(f"Serving {a.site} on http://{a.host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
