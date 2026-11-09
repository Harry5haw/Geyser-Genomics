"""Motion motifs (decorative): DeepVariant's net beside its credit, AlphaGenome's base strip in its panel header."""

import os
import re

import pytest

from samples.status import deepvariant_state

from .test_web_alphagenome import fixture_text, record, sample_with

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "samples", "templates", "samples")
DV_CREDIT = "Variant calling: DeepVariant (Google)"


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


def test_a_deepvariant_run_shows_the_settled_net_with_its_credit(client, storage):
    rec = record()
    rec["stages"]["DeepVariant"] = {"status": "Success", "started": "2026-11-04T10:01:00Z"}
    sample_with(storage, run_record=rec)
    page = client.get("/samples/S1/").content.decode()
    assert '<p class="mdv done">' in page and DV_CREDIT in page and "calling…" not in page


def test_a_bcftools_run_has_no_deepvariant_motif(client, storage):
    sample_with(storage, run_record=record())
    page = client.get("/samples/S1/").content.decode()
    assert "mdv" not in page.split("<style>")[0] + page.split("</style>")[-1] and DV_CREDIT not in page


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
    block = css
    for frames in re.findall(r"@keyframes [\w-]+\{(.*?\})\}", block):
        props = set(re.findall(r"([a-z-]+):", frames))
        assert props <= {"transform", "opacity"}, props
    for shared in ("_site.css", "_samples.css"):  # the detail page's own sheet only: 0 bytes on the other pages
        assert "mdv" not in read(shared) and "mag-" not in read(shared)
    assert "@media (prefers-reduced-motion:reduce)" in read("_site.css")  # stops every animation, these included


@pytest.mark.parametrize("name", ["_motif_dv.html", "_motif_ag.html"])
def test_the_motif_comments_never_reach_the_page(name):
    # {# #} is one line only in Django: a multi-line note must be {% comment %}
    assert "{#" not in read(name)
