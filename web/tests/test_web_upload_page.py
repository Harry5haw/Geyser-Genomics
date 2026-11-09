"""The hosted site's upload page (/uploads/, v1.4): published with an API base only, token-gated by the web API.

The page's logic is static/samples/upload.js (tested in node: web/tests/js/upload.test.js); these tests cover
what the publisher renders: when the page exists, the choices it offers, and that they match the pipeline's.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings as live_settings
from django.core.management import call_command

from samples import publish as publish_mod
from samples.static_site import GATE_PROFILES, reference_names

from .web_helpers import fastq_gz, put

API = "https://api.example.test"
REPO = Path(live_settings.REPO_ROOT)


@pytest.fixture
def site(storage, tmp_path, settings):
    settings.GEYSER_FIGURES_DIR = tmp_path / "no-figures-here"
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    for key in ("reference/chr20.fa", "reference/chr20.fa.fai", "reference/GRCh38.fa", "reference/GRCh38.fa.0123",
                "reference/sub/other.fa", "reference/notes.txt"):  # fmt: skip
        put(storage, key, b"x")
    return tmp_path / "site"


def publish(out, *args):
    call_command("publish", "--all", "--out", str(out), *args)
    return Path(out)


def test_the_upload_page_is_published_only_with_an_api(site, tmp_path):
    assert not (publish(tmp_path / "no-api") / "uploads").exists()
    assert 'href="/uploads/"' not in (tmp_path / "no-api/index.html").read_text(encoding="utf-8")
    root = publish(site, "--api-base", API)
    assert (root / "uploads/index.html").is_file()
    assert 'href="/uploads/"' in (root / "index.html").read_text(encoding="utf-8")


def test_the_live_ui_has_no_upload_page(client, storage):
    assert client.get("/uploads/").status_code == 404  # its uploads go through the form on the list


def test_the_page_offers_the_pipelines_choices(site):
    page = (publish(site, "--api-base", API) / "uploads/index.html").read_text(encoding="utf-8")
    form = re.search(r'<form id="upload-form"[^>]*>', page).group()
    assert f'data-api="{API}"' in form and 'data-existing="S1"' in form
    gate = re.search(r'<select id="up-gate">(.*?)</select>', page, re.S).group(1)
    assert re.findall(r'value="([^"]*)"', gate) == ["", "synthetic", "smoke"]  # "" = the run's default profile
    reference = re.search(r'<select id="up-reference">(.*?)</select>', page, re.S).group(1)
    assert re.findall(r'value="([^"]*)"', reference) == ["", "GRCh38.fa"]  # chr20.fa is the default ("")
    assert "The default (chr20.fa)" in reference
    assert re.search(r'<script src="/static/samples/upload\.[0-9a-f]{12}\.js" defer>', page)
    assert not re.search(r"\sstyle=", page)


def test_reference_names_are_the_fastas_directly_under_reference():
    keys = ["reference/a.fa", "reference/a.fa.fai", "reference/b.fasta.gz", "reference/b.fasta.gz.gzi",
            "reference/x/c.fa", "reference/README", "reference/", "other/d.fa"]  # fmt: skip
    assert reference_names(keys) == ["a.fa"]  # a gzipped FASTA isn't one the stages can index


def test_the_gate_profiles_are_the_gates_and_the_state_machines():
    names = [name for name, _ in GATE_PROFILES]
    assert names[0] == "default"
    assert sorted(names) == sorted(p.stem for p in (REPO / "gate" / "thresholds").glob("*.toml"))
    asl = (REPO / "infrastructure" / "state_machine.asl.json").read_text(encoding="utf-8")
    check = json.loads(re.sub(r"\$\{[^}]+\}", "0", asl))["States"]["Check_Gate_Profile"]  # templatefile() slots
    accepted = re.findall(r'"\$\.gate_profile", "StringEquals": "([^"]+)"', json.dumps(check))
    assert sorted(accepted) == sorted(names)


def test_a_one_sample_publish_invalidates_the_upload_page(site, monkeypatch):
    from .test_web_publish import FakeS3

    fake, invalidations = FakeS3(), []
    monkeypatch.setattr(publish_mod, "writer_for", lambda out: publish_mod.S3Writer("b", client=fake))
    monkeypatch.setattr(publish_mod, "_invalidate", lambda dist, paths: invalidations.append(paths))
    monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E1")
    call_command("publish", "S1", "--out", "s3://b", "--api-base", API)
    assert "uploads/index.html" in fake.objects and "/uploads/*" in invalidations[0]
