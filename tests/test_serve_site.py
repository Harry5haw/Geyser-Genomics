"""scripts/serve_site.py: a published site folder served as S3 + CloudFront serve it (for web-perf.yml)."""

import ast
import gzip
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import serve_site
from conftest import REPO_ROOT

HASHES = {"script": ["sha256-AAA="], "style": ["sha256-BBB=", "sha256-CCC="]}


def publish_constants() -> dict:
    """The cache values in web/samples/publish.py, read without Django."""
    with open(os.path.join(REPO_ROOT, "web", "samples", "publish.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("IMMUTABLE", "HTML_CACHE", "DATA_CACHE"):
                found[name] = ast.literal_eval(node.value)
            elif name in ("HASHED_NAME", "TYPES"):
                found[name] = ast.unparse(node.value)
    return found


def test_the_cache_rules_are_the_publishers():
    published = publish_constants()
    assert published["IMMUTABLE"] == serve_site.IMMUTABLE
    assert published["HTML_CACHE"] == serve_site.HTML_CACHE
    assert published["DATA_CACHE"] == serve_site.DATA_CACHE
    assert published["HASHED_NAME"] == f"re.compile({serve_site.HASHED_NAME.pattern!r})"
    assert ast.literal_eval(published["TYPES"]) == serve_site.TYPES


@pytest.mark.parametrize(
    "path, expected",
    [
        ("static/samples/geyser.ff4496679024.js", serve_site.IMMUTABLE),
        ("index.html", serve_site.HTML_CACHE),
        ("samples/S1/calls/2/index.html", serve_site.HTML_CACHE),
        ("figures/turnaround.json", serve_site.DATA_CACHE),
        ("csp-hashes.json", serve_site.DATA_CACHE),
    ],
)
def test_cache_control_per_object(path, expected):
    assert serve_site.cache_control(path) == expected


def test_the_csp_is_the_hosted_sites():
    csp = serve_site.content_security_policy(HASHES, ["https://api.example", "https://lake.s3.eu-west-2.amazonaws.com"])
    assert csp == (
        "default-src 'none'; script-src 'self' 'sha256-AAA='; style-src 'self' 'sha256-BBB=' 'sha256-CCC='; "
        "img-src 'self' data:; connect-src 'self' https://api.example https://lake.s3.eu-west-2.amazonaws.com; "
        "worker-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    assert "unsafe-inline" not in csp


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    for rel, text in {
        "index.html": "<p>list</p>",
        "404.html": "<p>not found</p>",
        "uploads/index.html": "<p>upload</p>",
        "static/samples/geyser.ff4496679024.js": "/* js */",
        "csp-hashes.json": json.dumps(HASHES),
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    csp = serve_site.content_security_policy(HASHES)
    server = serve_site.Server(("127.0.0.1", 0), serve_site.make_handler(str(root), csp, "/api"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", csp
    server.shutdown()
    server.server_close()


def get(url, gzip_ok=False):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"} if gzip_ok else {})
    try:
        with urllib.request.urlopen(request) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_folders_are_their_index_with_the_page_headers(site):
    base, csp = site
    status, headers, body = get(base + "/uploads/")
    assert status == 200 and body == b"<p>upload</p>"
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == serve_site.HTML_CACHE
    assert headers["Content-Security-Policy"] == csp
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_text_is_gzipped_when_asked(site):
    base, _ = site
    status, headers, body = get(base + "/static/samples/geyser.ff4496679024.js", gzip_ok=True)
    assert status == 200 and headers["Content-Encoding"] == "gzip" and gzip.decompress(body) == b"/* js */"
    assert headers["Cache-Control"] == serve_site.IMMUTABLE
    assert "Content-Encoding" not in get(base + "/static/samples/geyser.ff4496679024.js")[1]


@pytest.mark.parametrize("path", ["/samples/NOPE/", "/../secret.txt", "/%2e%2e/secret.txt", "/static/../../secret.txt"])
def test_missing_paths_and_paths_outside_the_site_are_the_404_page(site, path):
    base, _ = site
    status, headers, body = get(base + path)
    assert status == 404 and body == b"<p>not found</p>"
    assert headers["Cache-Control"] == serve_site.HTML_CACHE


def test_the_stub_api_answers_a_run_that_ended_long_ago(site):
    base, _ = site
    status, headers, body = get(base + "/api/status/SYNTH001")
    run = json.loads(body)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert run["sample_id"] == "SYNTH001" and run["status"] == "SUCCEEDED"
    assert run["stopped"].endswith("+00:00")  # ISO 8601 UTC, as the web API answers


def test_a_folder_that_isnt_a_published_site_is_refused(tmp_path, capsys):
    assert serve_site.main([str(tmp_path), "--port", "0"]) == 1
    assert "isn't a published site" in capsys.readouterr().err
