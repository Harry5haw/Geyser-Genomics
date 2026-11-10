"""Motion motifs (decorative): DeepVariant's net beside its credit, AlphaGenome's base strip in its panel header."""

import os
import re

import pytest

from samples.status import deepvariant_state

from .test_web_alphagenome import fixture_text, record, sample_with

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
        # the DeepVariant nodes hand their colour on (the user's choice): fill only, on small circles
        allowed = {"fill"} if name.startswith("mdv-c") else {"transform", "opacity"}
        assert props <= allowed, (name, props)
    for shared in ("_site.css", "_samples.css"):  # the detail page's own sheet only: 0 bytes on the other pages
        assert "mdv" not in read(shared) and "mag-" not in read(shared)
    assert "@media (prefers-reduced-motion:reduce)" in read("_site.css")  # stops every animation, these included


@pytest.mark.parametrize("name", ["_motif_dv.html", "_motif_ag.html"])
def test_the_motif_comments_never_reach_the_page(name):
    # {# #} is one line only in Django: a multi-line note must be {% comment %}
    assert "{#" not in read(name)


def test_a_finished_run_plays_once_then_rests_and_hover_replays_it():
    css = read("_motifs.css")
    assert ".mdv.done .k1{animation:mdv-c1 1.6s linear}" in css  # one pass of the colour hand-off, not infinite
    assert "@keyframes mdv-c1{0%{fill:var(--c1)}" in css and "100%{fill:var(--c1)}}" in css  # ends at rest
    assert ".ok" not in css and "<path" not in read("_motif_dv.html").split("</defs>")[1]  # no tick any more
    assert "infinite" not in "".join(rule for rule in css.split("}") if ".mdv.done" in rule)
    with open(os.path.join(TEMPLATES, "..", "..", "static", "samples", "geyser.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert '".dv-card"' in js and '"mouseenter"' in js and '"focusin"' in js


def test_the_net_fills_the_heading_row_as_one_interconnected_network():
    svg = read("_motif_dv.html")
    assert 'viewBox="0 0 960 50" preserveAspectRatio="xMinYMid slice" height="50"' in svg  # fixed height: no shift
    edge = re.search(r'<path id="dv-e" d="([^"]+)"', svg).group(1)
    ends = {tuple(map(int, m)) for m in re.findall(r"L(\d+) (\d+)", edge)}
    assert {x for x, _ in ends} == {48, 80}  # each column joins the next, and the period's last joins the next period
    assert svg.count('<use href="#dv-e"') == 15 and svg.count("<circle") == 15 * 7
