"""Site navigation (nav-spec, 12 Nov 2026): the header and footer links, the current page, the sample page's
breadcrumb, "On this page" chips and previous/next links, and the How it works page, live and published."""

import re

from django.core.management import call_command

from samples import status as status_mod
from samples.context_processors import GITHUB_URL, nav_here

from .web_helpers import fastq_gz, metric, put, vcf_gz, write_metrics

HEADER_NAV = re.compile(r'<nav class="site-nav" aria-label="Site">(.*?)</nav>', re.S)
ONPAGE = re.compile(r'<nav class="onpage" aria-label="On this page">(.*?)</nav>', re.S)


def finished(storage, sample_id, finished_at):
    """A sample with every stage's output, its run last active at finished_at (the list's order)."""
    put(storage, f"raw_reads/{sample_id}.fastq.gz", fastq_gz())
    put(storage, f"qc_reports/{sample_id}.fastp.html", b"<html></html>")
    put(storage, f"alignments/{sample_id}.cram", b"x")
    put(storage, f"variants/{sample_id}.vcf.gz", vcf_gz([(100, "A", "G")]))
    return [metric(t, sample_id, timestamp=finished_at) for t in ("QualityControl", "Align", "CallVariants")]


def three_samples(storage):
    """OLD, MID and NEW, finished in that order: the list shows NEW, MID, OLD."""
    write_metrics(
        storage, finished(storage, "OLD", 1000.0) + finished(storage, "MID", 2000.0) + finished(storage, "NEW", 3000.0)
    )


def header(body: str) -> str:
    (nav,) = HEADER_NAV.findall(body)
    return nav


def test_nav_here_follows_the_path():
    assert nav_here("/") == "samples"
    assert nav_here("/samples/S1/") == "sample" and nav_here("/samples/S1/calls/2/") == "sample"
    assert nav_here("/figures/") == "figures" and nav_here("/about/") == "about"
    assert nav_here("/uploads/") == "upload" and nav_here("/404.html") == ""


def test_the_header_marks_the_current_page(client, storage):
    three_samples(storage)
    assert 'aria-current="page">Samples' in header(client.get("/").content.decode())
    assert 'aria-current="page">How it works' in header(client.get("/about/").content.decode())
    sample = header(client.get("/samples/MID/").content.decode())
    assert 'aria-current="true">Samples' in sample and 'aria-current="page"' not in sample
    assert f'href="{GITHUB_URL}"' in sample


def test_every_page_has_a_skip_link_to_main_and_a_footer_nav(client, storage):
    body = client.get("/about/").content.decode()
    assert '<a class="skip" href="#main">' in body and '<main class="wrap" id="main">' in body
    assert '<nav aria-label="Footer">' in body and "AlphaGenome output terms" in body


def test_live_upload_link_goes_to_the_form_on_the_list(client, storage):
    nav = header(client.get("/about/").content.decode())
    assert 'href="/#upload">Upload' in nav
    assert 'id="upload"' in client.get("/").content.decode()


def test_a_sample_page_has_a_breadcrumb_chips_and_its_neighbours_in_list_order(client, storage):
    three_samples(storage)
    body = client.get("/samples/MID/").content.decode()
    assert '<li aria-current="page">MID</li>' in body
    (chips,) = ONPAGE.findall(body)
    assert re.findall(r'href="#([\w-]+)"', chips) == ["run-status", "variants", "qc", "files"]
    for anchor in ("run-status", "variants", "qc", "files"):
        assert f'id="{anchor}"' in body  # every chip has its section
    assert 'href="/samples/NEW/" rel="prev">← NEW' in body and 'href="/samples/OLD/" rel="next">OLD →' in body
    first = client.get("/samples/NEW/").content.decode()
    assert 'rel="prev"' not in first and 'href="/samples/MID/" rel="next"' in first


def test_published_neighbours_skip_a_running_sample_and_the_site_has_about(storage, tmp_path, settings):
    settings.GEYSER_FIGURES_DIR = tmp_path / "no-figures-here"
    three_samples(storage)
    put(storage, "raw_reads/RUNNING.fastq.gz", fastq_gz())  # uploaded, nothing run yet: no page to link to
    out = tmp_path / "site"
    call_command("publish", "--all", "--out", str(out))
    new = (out / "samples/NEW/index.html").read_text(encoding="utf-8")
    assert "RUNNING" not in new.split('aria-label="Previous and next sample"', 1)[1]
    about = (out / "about/index.html").read_text(encoding="utf-8")
    assert "<h1>How it works</h1>" in about
    # No web API: nothing to upload to, so no Upload link anywhere on the published site.
    assert ">Upload<" not in header(about)


def test_published_upload_link_only_with_an_api(storage, tmp_path, settings):
    settings.GEYSER_FIGURES_DIR = tmp_path / "no-figures-here"
    three_samples(storage)
    out = tmp_path / "site"
    call_command("publish", "--all", "--out", str(out), "--api-base", "https://api.example")
    nav = header((out / "about/index.html").read_text(encoding="utf-8"))
    assert 'href="/uploads/">Upload' in nav


def test_list_order_is_the_lists_order_without_building_samples(storage):
    three_samples(storage)
    put(storage, "raw_reads/FRESH.fastq.gz", fastq_gz())  # no metrics yet: last
    assert status_mod.list_order(storage) == [s.sample_id for s in status_mod.load_all(storage)]
    assert status_mod.list_order(storage)[:3] == ["NEW", "MID", "OLD"]
