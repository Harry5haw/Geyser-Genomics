"""Stage status as a double helix (samples/_dna.html, the sprite in samples/_dna_sprite.html, the CSS in _site.css)."""

import os
import re

import pytest
from django.template.loader import render_to_string

from .test_web_theme import CSS, contrast, theme
from .web_helpers import fastq_gz, put

STATES = ("running", "done", "failed", "pending", "skipped")


@pytest.mark.parametrize("state", STATES)
def test_every_state_renders_a_labelled_helix(state):
    svg = render_to_string("samples/_dna.html", {"state": state, "name": "align", "label": "Alignment"})
    assert f'class="dna {state} s-align"' in svg and f'aria-label="Alignment: {state}"' in svg and 'role="img"' in svg
    assert ('href="#dna-break"' in svg) == (state == "failed")  # a failed stage's helix is broken apart
    assert "style=" not in svg  # the hosted CSP allows no inline style


def test_the_sprite_is_on_the_pages_that_show_stage_status_only(client, storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    listing = client.get("/").content.decode()
    detail = client.get("/samples/S1/").content.decode()
    figures = client.get("/figures/").content.decode()
    for page in (listing, detail):
        assert page.count('<symbol id="dna"') == 1 and page.count('<symbol id="dna-break"') == 1
    assert "<symbol" not in figures  # the figures page has no stage status (and the tightest size budget)
    # the sample page: one helix per stage in the track and one in each stage card's pill, each with a text label
    assert len(re.findall(r'<svg class="dna ', detail)) == 6 and "Quality control" in detail


def test_motion_is_paused_unless_running_and_stops_under_reduced_motion():
    with open(CSS, encoding="utf-8") as fh:
        css = fh.read()
    with open(CSS.replace("_site.css", "_samples.css"), encoding="utf-8") as fh:
        css += fh.read()  # the helix rules are the sample pages' CSS; reduced motion is global
    # Only a running stage's helix turns (the <use> element itself: CSS can't reach inside it); the rest stay still.
    animated = re.findall(r"([^{}]+)\{animation:dna-turn", css)
    assert [sel.strip() for sel in animated] == [".dna.running use"]
    assert re.search(r"@media \(prefers-reduced-motion:reduce\)\{\*,::before,::after\{animation:none!important", css)


@pytest.mark.parametrize("name", ["light", "dark", "retro", "contrast"])
def test_every_helix_colour_stands_out_from_the_cards(name):
    # Graphics need 3:1 against what's behind them (WCAG 1.4.11): the card and the page.
    t = theme(name)
    colours = [t[k] for k in ("helix-qc", "helix-align", "helix-variants", "done", "fail", "pend")]
    assert all(contrast(c, t[bg]) >= 3 for c in colours for bg in ("card", "bg")), name


def test_the_partial_and_sprite_exist_as_files():
    here = os.path.join(os.path.dirname(__file__), "..", "samples", "templates", "samples")
    assert os.path.isfile(os.path.join(here, "_dna.html")) and os.path.isfile(os.path.join(here, "_dna_sprite.html"))
