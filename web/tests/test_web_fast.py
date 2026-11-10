"""W1, "fast like Web 1.0": what keeps the pages small and instant (web plan W1).

Speculation rules, hashed immutable assets, the per-page weight budget, the paged tables, HTML-only gzip and no
framework. Lighthouse measures the result; these tests keep it from regressing.
"""

import gzip
import json
import re

import pytest
from django.core.management import call_command
from django.test import override_settings

from .web_helpers import FASTP_SINGLE, fastq_gz, gz, put

PAGE_BUDGET_BYTES = 30 * 1024  # HTML + CSS + JS per page, as transferred (gzip), the W1 target
SPECULATION = re.compile(r'<script type="speculationrules">(.*?)</script>', re.S)
HEADER = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"


def big_sample(storage, calls=1500):
    """A finished sample with a long VCF, as the slowest real page has."""
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "qc_reports/S1.fastp.html", b"<html></html>")
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    put(storage, "alignments/S1.cram", b"x")
    records = "".join(f"synth1\t{1000 + 37 * i}\t.\tA\tG\t99.5\t.\tDP=30\tGT\t0/1\n" for i in range(calls))
    put(storage, "variants/S1.vcf.gz", gz((HEADER + records).encode()))


def speculation_rules(body: str) -> dict:
    (rules,) = SPECULATION.findall(body)
    return json.loads(rules)


# ------------------------------------------------------------------ speculation rules


def test_every_page_prerenders_same_site_links_on_hover(client, storage):
    big_sample(storage)
    for url in ("/", "/samples/S1/"):
        rules = speculation_rules(client.get(url).content.decode())
        (rule,) = rules["prerender"]
        assert rule["eagerness"] == "moderate"  # on hover / pointer-down, not every link up front
        clauses = rule["where"]["and"]
        # Same-site only. URL patterns match the query string separately: "/*" alone would leave out ?vpage= pages.
        assert {"or": [{"href_matches": "/*"}, {"href_matches": "/*\\?*"}]} in clauses


def test_a_static_root_without_collectstatic_still_serves_pages(client, storage, tmp_path, settings):
    # The hashed storage is on whenever STATIC_ROOT is set; a missing manifest falls back to plain names, not a 500.
    from django.test import override_settings

    (root := tmp_path / "never-collected").mkdir()  # exists, but nothing was collected into it
    with override_settings(
        DEBUG=False,
        STATIC_ROOT=root,
        STORAGES={"staticfiles": {"BACKEND": "geyser_web.static_storage.ForgivingManifestStaticFilesStorage"}},
    ):
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'src="/static/samples/geyser.js"' in resp.content.decode()


def test_a_file_missing_from_a_stale_manifest_is_served_plain(client, storage, tmp_path):
    # Collected before geyser.js existed (simulated: its manifest entry removed). Hashing it on the fly would name a
    # file collectstatic never wrote (a 404); the plain name loads from the app folders instead.
    from django.test import override_settings

    root = tmp_path / "static"
    with override_settings(
        DEBUG=False,
        STATIC_ROOT=root,
        STORAGES={"staticfiles": {"BACKEND": "geyser_web.static_storage.ForgivingManifestStaticFilesStorage"}},
    ):
        call_command("collectstatic", interactive=False, verbosity=0)
        manifest = json.loads((root / "staticfiles.json").read_text(encoding="utf-8"))
        del manifest["paths"]["samples/geyser.js"]
        (root / "staticfiles.json").write_text(json.dumps(manifest), encoding="utf-8")
        from django.contrib.staticfiles.storage import staticfiles_storage

        staticfiles_storage.hashed_files, _ = staticfiles_storage.load_manifest()  # (paths, manifest hash)
        body = client.get("/").content.decode()
        assert 'src="/static/samples/geyser.js"' in body
        assert re.search(r'src="/static/samples/geyser-lockup\.[0-9a-f]{12}\.svg"', body)  # the rest stay hashed
        asset = client.get("/static/samples/geyser.js")
        assert asset.status_code == 200
        asset.close()


def test_a_zero_page_size_is_treated_as_one(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 0
    big_sample(storage, calls=3)
    body = client.get("/samples/S1/?vpage=9").content.decode()
    assert "3–3 of 3 calls" in body  # a page past the end shows the last one, with no division by zero


@pytest.mark.parametrize(
    "path", ["/samples/*/files/*", "/samples/*/status/*", "/samples/rows/*", "/upload/*", "/demo/*"]
)
def test_downloads_posts_and_fragments_are_never_prerendered(client, storage, path):
    rules = speculation_rules(client.get("/").content.decode())
    assert {"not": {"href_matches": path}} in rules["prerender"][0]["where"]["and"]


def test_download_links_are_excluded_by_selector_too(client, storage):
    big_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    selectors = [
        c["not"]["selector_matches"]
        for c in speculation_rules(body)["prerender"][0]["where"]["and"]
        if "selector_matches" in c.get("not", {})
    ]
    assert selectors and "[download]" in selectors[0] and "[target=_blank]" in selectors[0]
    assert re.search(r'href="/samples/S1/files/vcf/" download', body)  # every file link carries the attribute


# ------------------------------------------------------------------ hashed, immutable assets


@pytest.fixture
def collected(tmp_path, storage):
    """collectstatic into a temporary STATIC_ROOT with the production storage (hashed names, .gz/.br)."""
    root = tmp_path / "static"
    with override_settings(
        DEBUG=False,
        STATIC_ROOT=root,
        STORAGES={"staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}},
    ):
        call_command("collectstatic", interactive=False, verbosity=0)
        yield root


def test_assets_get_content_hashed_names(client, collected):
    body = client.get("/").content.decode()
    for name, ext in (
        ("geyser", "js"),
        ("geyser-lockup", "svg"),
        ("geyser-icon", "svg"),
        ("favicon-32", "png"),
        ("apple-touch-icon", "png"),
    ):
        assert re.search(rf'/static/samples/{name}\.[0-9a-f]{{12}}\.{ext}"', body), name
    assert (collected / "staticfiles.json").exists()


def test_hashed_assets_are_cached_for_good_and_precompressed(client, collected):
    body = client.get("/").content.decode()
    (js,) = re.findall(r'src="(/static/samples/geyser\.[0-9a-f]{12}\.js)"', body)
    resp = client.get(js, HTTP_ACCEPT_ENCODING="gzip")
    assert resp.status_code == 200
    assert "immutable" in resp["Cache-Control"] and "max-age=315360000" in resp["Cache-Control"]
    assert resp["Content-Encoding"] == "gzip"  # the precompressed copy, no work per request
    assert list(collected.glob("samples/geyser.*.js.gz"))  # (.br too where the brotli package is installed)


# ------------------------------------------------------------------ page weight


@pytest.mark.parametrize("url", ["/", "/samples/S1/"])
def test_each_page_fits_the_30_kb_budget_with_its_css_and_js(client, storage, url):
    big_sample(storage)
    body = client.get(url).content
    import samples

    with open(samples.__path__[0] + "/static/samples/geyser.js", "rb") as fh:
        js = fh.read()
    # The CSS is inside the HTML; the one script is the only other request (both as gzip sends them).
    transferred = len(gzip.compress(body)) + len(gzip.compress(js))
    assert transferred <= PAGE_BUDGET_BYTES, f"{url}: {transferred} bytes"


def test_no_framework_and_no_stylesheet_request(client, storage):
    big_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "<style>" in body and 'rel="stylesheet"' not in body
    assert "pico" not in body.lower() and "htmx" not in body.lower() and "hx-" not in body
    # The theme boot inline (before the first paint, no request); theme.js and geyser.js deferred.
    assert len(re.findall(r"<script src=", body)) == 2
    assert re.search(r'<script src="[^"]+theme[^"]*\.js" defer>', body)
    assert re.search(
        r"<script>\(function \(\) \{ var t = null.*?setAttribute\(\"data-theme\", t\); \}\)\(\);</script>", body
    )
    assert re.search(r'<script src="[^"]+geyser[^"]*\.js" defer>', body)


def test_the_inlined_css_has_no_comments(client, storage):
    style = re.search(r"<style>(.*?)</style>", client.get("/").content.decode(), re.S).group(1)
    assert "/*" not in style and "--primary" in style
    # four themes and the header's logo eruption; a guard only: the real budget is the gzipped page (HTML+CSS+JS
    # <= 30 KB, Lighthouse's check) and inlined CSS gzips to ~27%. Raised 20 -> 24 KB as agreed with T3 (9 Nov 2026).
    assert len(style) < 24 * 1024


def test_images_have_their_size_so_nothing_shifts(client, storage):
    body = client.get("/").content.decode()
    imgs = re.findall(r"<img [^>]+>", body)  # the lockup per theme; CSS shows one
    assert len(imgs) == 4
    for img in imgs:
        assert 'width="180"' in img and 'height="40"' in img and 'alt="Geyser Genomics"' in img


# ------------------------------------------------------------------ paged tables


def test_the_first_page_of_calls_is_inline_and_the_rest_are_links(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "1500 calls" in body and "1–50 of 1500 calls" in body
    assert body.count("<td>synth1</td>") == 50
    # Pages are paths, not ?vpage=: the static site publishes each as a folder with an index.html (W2).
    assert 'href="/samples/S1/calls/2/#variants" rel="next"' in body
    assert 'rel="prev"' not in body


def test_a_later_page_and_its_neighbours(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/calls/3/").content.decode()
    assert "101–150 of 1500 calls" in body
    assert f'<td class="num">{1000 + 37 * 100}</td>' in body  # call 101 starts the page
    assert 'href="/samples/S1/calls/2/#variants" rel="prev"' in body
    assert 'href="/samples/S1/calls/4/#variants" rel="next"' in body


def test_the_old_query_page_links_still_open_the_right_page(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/?vpage=3").content.decode()  # links from before W2 (bookmarks)
    assert "101–150 of 1500 calls" in body and 'href="/samples/S1/calls/4/#variants" rel="next"' in body


def test_page_two_links_back_to_the_plain_url(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/?vpage=2").content.decode()
    assert 'href="/samples/S1/#variants" rel="prev"' in body


@pytest.mark.parametrize("page", ["999", "0", "-3", "two"])
def test_out_of_range_pages_show_a_real_page(client, storage, settings, page):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get(f"/samples/S1/?vpage={page}").content.decode()
    expected = "1451–1500 of 1500 calls" if page == "999" else "1–50 of 1500 calls"
    assert expected in body


def test_short_tables_have_no_pager(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage, calls=10)
    assert 'class="pager"' not in client.get("/samples/S1/").content.decode()


# ------------------------------------------------------------------ compression


def test_html_is_gzipped_and_downloads_are_not(client, storage):
    big_sample(storage)
    page = client.get("/samples/S1/", HTTP_ACCEPT_ENCODING="gzip, br")
    assert page["Content-Encoding"] == "gzip"
    download = client.get("/samples/S1/files/vcf/", HTTP_ACCEPT_ENCODING="gzip, br")
    assert not download.has_header("Content-Encoding")  # a .vcf.gz is sent as it is, never recompressed
    download.close()  # a FileResponse holds the file open until closed


def test_the_svg_favicon_comes_first_with_the_png_as_fallback(client, storage):
    head = client.get("/").content.decode().split("</head>")[0]
    icons = re.findall(r'<link rel="icon" type="([^"]+)"[^>]*href="([^"]+)"', head)
    assert [t for t, _ in icons] == ["image/svg+xml", "image/png"]
    assert "geyser-icon" in icons[0][1] and "favicon-32" in icons[1][1]
    assert 'rel="apple-touch-icon"' in head
