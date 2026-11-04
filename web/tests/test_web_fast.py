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


def big_sample(storage, calls=1500, scored=120):
    """A finished sample with a long VCF and a long AlphaGenome table, as the slowest real page has."""
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "qc_reports/S1.fastp.html", b"<html></html>")
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    put(storage, "alignments/S1.cram", b"x")
    records = "".join(f"synth1\t{1000 + 37 * i}\t.\tA\tG\t99.5\t.\tDP=30\tGT\t0/1\n" for i in range(calls))
    put(storage, "variants/S1.vcf.gz", gz((HEADER + records).encode()))
    rows = "".join(
        f"{i + 1}\tsynth1\t{1000 + 37 * i}\tA\tG\tsnv\t99.5\t{40 - i * 0.2:.2f}\t0.5\tRNA_SEQ:liver\tSYNTHG{i}\t0.3\tupstream\tatlas\n"
        for i in range(scored)
    )
    put(
        storage,
        "stats/S1/alphagenome.tsv",
        "rank\tchrom\tpos\tref\talt\tkind\tqual\tavi\tavi_raw\ttop_track\tgene\trna_lfc\tcsq\tsource\n" + rows,
    )


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
        assert {"href_matches": "/*"} in clauses  # same-site only


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
    for name, ext in (("geyser", "js"), ("geyser-mark", "png"), ("favicon-32", "png"), ("apple-touch-icon", "png")):
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
    assert re.findall(r"<script src=", body) == ["<script src="]  # geyser.js, deferred
    assert re.search(r'<script src="[^"]+geyser[^"]*\.js" defer>', body)


def test_the_inlined_css_has_no_comments(client, storage):
    style = re.search(r"<style>(.*?)</style>", client.get("/").content.decode(), re.S).group(1)
    assert "/*" not in style and "--mint" in style and len(style) < 14 * 1024


def test_images_have_their_size_so_nothing_shifts(client, storage):
    body = client.get("/").content.decode()
    (img,) = re.findall(r"<img [^>]+>", body)
    assert 'width="62"' in img and 'height="72"' in img and 'alt=""' in img


# ------------------------------------------------------------------ paged tables


def test_the_first_page_of_calls_is_inline_and_the_rest_are_links(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "1500 calls" in body and "1–50 of 1500 calls" in body
    assert body.count("<td>synth1</td>") == 100  # 50 calls + 50 scored variants
    assert 'href="/samples/S1/?vpage=2#variants" rel="next"' in body
    assert 'rel="prev"' not in body.split('id="alphagenome"')[0]


def test_a_later_page_and_its_neighbours(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/?vpage=3&avi=asc").content.decode()
    variants = body.split('id="alphagenome"')[0]
    assert "101–150 of 1500 calls" in variants
    assert f'<td class="num">{1000 + 37 * 100}</td>' in variants  # call 101 starts the page
    assert 'href="/samples/S1/?vpage=2&amp;avi=asc#variants" rel="prev"' in variants  # other settings kept
    assert 'href="/samples/S1/?vpage=4&amp;avi=asc#variants" rel="next"' in variants


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


def test_the_alphagenome_table_pages_on_its_own(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage)
    body = client.get("/samples/S1/?agpage=2").content.decode()
    scored = body.split('id="alphagenome"')[1]
    assert "51–100 of 120 scored variants, by AVI" in scored
    assert 'href="/samples/S1/?agpage=3#alphagenome" rel="next"' in scored
    assert "1–50 of 1500 calls" in body  # the variant table stays on its first page
    # Re-sorting starts from the first page.
    assert 'href="/samples/S1/?avi=asc#alphagenome"' in scored


def test_short_tables_have_no_pager(client, storage, settings):
    settings.GEYSER_TABLE_ROWS = 50
    big_sample(storage, calls=10, scored=5)
    assert 'class="pager"' not in client.get("/samples/S1/").content.decode()


# ------------------------------------------------------------------ compression


def test_html_is_gzipped_and_downloads_are_not(client, storage):
    big_sample(storage)
    page = client.get("/samples/S1/", HTTP_ACCEPT_ENCODING="gzip, br")
    assert page["Content-Encoding"] == "gzip"
    download = client.get("/samples/S1/files/vcf/", HTTP_ACCEPT_ENCODING="gzip, br")
    assert not download.has_header("Content-Encoding")  # a .vcf.gz is sent as it is, never recompressed
