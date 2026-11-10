"""The sample list's shortcuts: DeepVariant / AlphaGenome / Failed filters and each row's tags."""

import json
import os
import re

from samples.status import SampleView

from .test_web_theme import CSS
from .web_helpers import fastq_gz, put


def test_tags_follow_the_run():
    s = SampleView("S1", "failed", [], keys={"vcf": "variants_deepvariant/S1.vcf.gz"})
    assert s.caller == "deepvariant" and s.tags == "deepvariant failed"
    s = SampleView("S2", "succeeded", [], keys={"vcf": "variants/S2.vcf.gz"}, annotated=True, deepvariant=True)
    assert s.caller == "bcftools" and s.tags == "deepvariant alphagenome"  # DeepVariant ran, bcftools was the caller
    assert SampleView("S3", "succeeded", [], keys={"vcf": "variants/S3.vcf.gz"}).tags == ""


def test_the_list_tags_rows_from_the_run_record_parts_and_offers_the_filters(storage, client):
    for sid in ("DV1", "AG1", "PLAIN"):
        put(storage, f"raw_reads/{sid}.fastq.gz", fastq_gz())
    put(storage, "runs/DV1.provenance/DeepVariant.json", json.dumps({"task": "DeepVariant", "status": "Success"}))
    put(storage, "runs/AG1.provenance/Annotate.json", json.dumps({"task": "Annotate", "status": "Success"}))
    body = client.get("/").content.decode()
    rows = dict(re.findall(r'<tr data-tags="([^"]*)"[^>]*>\s*<td class="id"><a href="/samples/(\w+)/"', body))
    assert {v: k for k, v in rows.items()} == {"DV1": "deepvariant", "AG1": "alphagenome", "PLAIN": ""}
    assert '<span class="tag">DeepVariant</span>' in body and '<span class="tag">AlphaGenome</span>' in body
    group = re.search(r'<div class="filters" role="group" aria-label="Show samples">(.*?)</div>', body, re.S)
    assert group and re.findall(r'data-filter-choice="(\w+)" aria-pressed="(\w+)"', group.group(1)) == [
        ("all", "true"),
        ("deepvariant", "false"),
        ("alphagenome", "false"),
        ("failed", "false"),
    ]  # invisible (space kept: no layout shift) until geyser.js sets data-ready: without it they'd do nothing


def test_css_hides_the_other_rows_for_each_filter_and_the_script_switches_it():
    with open(os.path.join(os.path.dirname(CSS), "_samples.css"), encoding="utf-8") as fh:
        css = fh.read()
    for tag in ("deepvariant", "alphagenome", "failed"):
        assert f"#samples[data-filter={tag}] tbody tr:not([data-tags~={tag}])" in css
    js_path = os.path.join(os.path.dirname(CSS), "..", "..", "static", "samples", "geyser.js")
    with open(js_path, encoding="utf-8") as fh:
        js = fh.read()
    assert "dataset.filter = b.dataset.filterChoice" in js and 'setAttribute("aria-pressed"' in js
