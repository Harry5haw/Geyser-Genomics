"""Motion motifs (decorative): DeepVariant's net beside its credit, AlphaGenome's base strip in its panel header."""

import os
import re

import pytest

from samples.status import deepvariant_state

from .test_web_alphagenome import fixture_text, record, sample_with
from .web_helpers import fastq_gz, put, vcf_gz

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "samples", "templates", "samples")


def read(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize(
    "rec,run_state,expected",
    [
        (None, "succeeded", None),
        ({"stages": {"CallVariants": {"status": "Success"}}}, "succeeded", None),  # bcftools called it
        ({"stages": {"DeepVariant": {"status": "Success"}}}, "succeeded", "done"),
        ({"caller_outcome": "deepvariant", "stages": {}}, "succeeded", "done"),
        ({"stages": {"DeepVariant": {"started": "t"}}}, "running", "running"),  # its part is open
        ({"stages": {"DeepVariant": {"started": "t"}}}, "failed", None),  # never finished: no motif
        ({"stages": {"DeepVariant": {"status": "Failure"}}}, "succeeded", None),  # fell back to bcftools
        ({"caller_outcome": "fallback_bcftools", "stages": {"DeepVariant": {"status": "Failure"}}}, "succeeded", None),
    ],
)
def test_the_deepvariant_motif_only_for_runs_deepvariant_called(rec, run_state, expected):
    assert deepvariant_state(rec, run_state) == expected


def test_a_deepvariant_run_has_its_own_card_with_the_motif_by_the_heading(client, storage):
    rec = record(stats={"bcftools_stats": {"records": 12, "snps": 10, "indels": 2}})
    rec["stages"]["DeepVariant"] = {
        "status": "Success",
        "started": "2026-11-04T10:01:00Z",
        "duration_seconds": 754.0,
        "resources": {"cpu_seconds": 3000.0},
        "make_examples": {"split": True, "parts": 16, "examples": 2230000, "part_seconds": [1290.0, 1920.0]},
        "gpu": {"model": "NVIDIA L4", "utilization_mean_pct": 71},
    }
    sample_with(storage, run_record=rec)
    page = client.get("/samples/S1/").content.decode()
    card = page[page.index('<section class="panel dv-card"') :]
    card = card[: card.index("</section>")]
    assert re.search(r'<div class="mhead"><h2 id="deepvariant-heading">Variant calling · DeepVariant</h2>\s*<svg', card)
    assert 'class="mdv-art mdv done"' in card and "calling…" not in card
    for fact in (
        "16 parts on CPU, 21 m 30 s–32 m 00 s each",
        "2,230,000",
        "GPU NVIDIA L4, 71% mean utilisation",
        "No: DeepVariant made the calls",
        "12 (10 SNPs, 2 indels)",
        "12 m 34 s",
        "50 m 00 s",
    ):
        assert fact in card, fact
    assert "DeepVariant is Google's open-source caller; Geyser is independent." in card
    status = page[page.index('id="run-status"') :]
    assert "mdv" not in status[: status.index("</section>")]  # not in the run-status card any more


def test_an_older_deepvariant_record_shows_what_it_has(client, storage):
    rec = record()
    rec["stages"]["DeepVariant"] = {"status": "Success", "started": "2026-11-04T10:01:00Z"}
    sample_with(storage, run_record=rec)
    page = client.get("/samples/S1/").content.decode()
    assert 'id="deepvariant"' in page and "No: DeepVariant made the calls" in page and "make_examples" not in page


def test_a_bcftools_run_has_no_deepvariant_card(client, storage):
    sample_with(storage, run_record=record())
    page = client.get("/samples/S1/").content.decode()
    assert 'id="deepvariant"' not in page and "dv-card" not in page.split("</style>")[-1]


def test_the_alphagenome_panel_has_the_strip_its_credit_and_the_terms_notice(client, storage):
    sample_with(storage, tsv=fixture_text(), run_record=record(annotate={"status": "Success"}))
    page = client.get("/samples/S1/").content.decode()
    # inline in the heading row (.mhead), no separate credit line: the heading names AlphaGenome
    assert re.search(r'<div class="mhead"><h2 id="alphagenome-heading">.*?</h2>\s*<svg class="mag-art"', page, re.S)
    assert "Annotation: AlphaGenome" not in page
    assert "By using this information, you agree to AlphaGenome Output Terms of Use" in page


@pytest.mark.parametrize("name", ["_motif_dv.html", "_motif_ag.html"])
def test_the_motifs_are_decorative_inline_svg_only(name):
    text = read(name)
    svg = re.search(r"<svg[^>]*>", text).group(0)
    assert 'aria-hidden="true"' in svg and 'focusable="false"' in svg
    assert "<style" not in text and "<script" not in text and " style=" not in text  # the CSP: no inline styles


def test_motif_css_is_transform_and_opacity_only_and_sample_pages_only():
    css = read("_motifs.css")
    for name, frames in re.findall(r"@keyframes ([\w-]+)\{(.*?\})\}", css):
        props = set(re.findall(r"([a-z-]+):", frames))
        # the DeepVariant nodes hand their colour on (fill) and the signal comets move along their routes
        # (stroke-dashoffset): the user's choices; everything else is transform and opacity
        special = {"mdv-comet": {"stroke-dashoffset"}, "mdv-trail": {"stroke-dashoffset"}}
        allowed = {"fill"} if re.fullmatch(r"mdv-c\d", name) else special.get(name, {"transform", "opacity"})
        assert props <= allowed, (name, props)
    for shared in ("_site.css", "_samples.css"):  # the detail page's own sheet only: 0 bytes on the other pages
        assert "mdv" not in read(shared) and "mag-" not in read(shared)
    assert "@media (prefers-reduced-motion:reduce)" in read("_site.css")  # stops every animation, these included


def test_motif_css_only_on_sample_pages_that_show_a_motif(client, storage):
    """No DeepVariant card and no AlphaGenome panel: no motif CSS (about a fifth of the page's CSS, LCP)."""
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G")]))
    body = client.get("/samples/S1/").content.decode()
    assert "mdv" not in body and "mag-" not in body


@pytest.mark.parametrize("name", ["_motif_dv.html", "_motif_ag.html"])
def test_the_motif_comments_never_reach_the_page(name):
    # {# #} is one line only in Django: a multi-line note must be {% comment %}
    assert "{#" not in read(name)


def test_signals_travel_three_routes_left_to_right_continuously():
    css, svg = read("_motifs.css"), read("_motif_dv.html")
    # every DeepVariant card, finished or running, loops: a 4 s period (slowed from 1.6 s at the user's request)
    assert ".mdv .k1{animation:mdv-c1 4s linear infinite}" in css  # the other nodes: a subtle colour hand-off
    assert ".mdv .nodes circle{transform-box:fill-box;transform-origin:center;opacity:.6}" in css
    assert ".mdv.done" not in css and ".mdv.running" not in css  # no play-once or rest state
    assert ".mdv .nodes>g:nth-child(30)>*{animation-delay:3.88s}" in css  # one period across the 30 columns
    # three routes, one node per column: a comet head and a trail behind it, a third of a period apart
    routes = re.findall(r'<path id="dv-r(\d)" pathLength="100" d="M([^"]+)"', svg)
    assert [r for r, _ in routes] == ["1", "2", "3"]
    for _, d in routes:
        xs = [int(p.split()[0]) for p in d.split("L")]
        assert len(xs) == 30 and xs == sorted(xs) and xs[0] == 16 and xs[-1] == 944  # far left to far right
    assert svg.count('class="hd r') == 3 and svg.count('class="tr r') == 3
    assert "@keyframes mdv-comet{from{stroke-dashoffset:3}to{stroke-dashoffset:-100}}" in css
    assert "@keyframes mdv-trail{from{stroke-dashoffset:18}to{stroke-dashoffset:-85}}" in css  # ends at the head
    assert ".mdv .cm .tr{" in css  # beats .mdv .cm use, so the trail keeps its own keyframes
    assert ".mdv .cm .r2{animation-delay:-2.67s}" in css and ".mdv .cm .r3{animation-delay:-1.33s}" in css
    # each route node lights up as its comet passes (a glow per route node, in its column, phase-shifted per route)
    assert svg.count('<circle class="g r') == 3 * 30
    assert "@keyframes mdv-g2{0%,33.3%{opacity:0" in css and "@keyframes mdv-g3{0%,66.7%{opacity:0" in css
    assert ".ok" not in css
    with open(os.path.join(TEMPLATES, "..", "..", "static", "samples", "geyser.js"), encoding="utf-8") as fh:
        assert ".dv-card" not in fh.read()  # no JS for the motif


def test_the_net_fills_the_heading_row_as_one_interconnected_network():
    svg = read("_motif_dv.html")
    assert 'viewBox="0 0 960 50" preserveAspectRatio="xMinYMid slice" height="50"' in svg  # fixed height: no shift
    edge = re.search(r'<path id="dv-e" d="([^"]+)"', svg).group(1)
    ends = {tuple(map(int, m)) for m in re.findall(r"L(\d+) (\d+)", edge)}
    assert {x for x, _ in ends} == {48, 80}  # each column joins the next, and the period's last joins the next period
    assert svg.count('<use href="#dv-e"') == 15 and svg.count('<circle class="k') == 15 * 7


def test_retro_draws_the_signal_paths_as_a_fine_pen_line():
    css = read("_motifs.css")
    assert ":root[data-theme=retro] .mdv .cm .tr{stroke-width:1.2}" in css  # about half the other themes' width
    assert ":root[data-theme=retro] .mdv .nodes .g{fill:none;stroke:#000;stroke-width:1.2}" in css  # an ink ring
    # the resting net in light grey, so the moving pen lines stand out
    assert ":root[data-theme=retro] .mdv .edges{stroke:#c8c8c8;opacity:1}" in css
    assert ":root[data-theme=retro] .mdv{--c1:#8a8a8a;--c2:#fff;--c3:#8a8a8a;--c4:#fff}" in css


def test_retro_never_fills_the_route_paths():
    # an ink fill on the comets' <use> would fill the zig-zag polygons (a black band across the net)
    assert ":is(.r1,.r2,.r3){stroke:#000}" in read("_motifs.css") and "r3){stroke:#000;fill" not in read("_motifs.css")
