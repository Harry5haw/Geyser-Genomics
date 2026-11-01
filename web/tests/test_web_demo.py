import gzip
import json

import pytest

from .web_helpers import put
from samples import demo


def test_demo_button_creates_reference_reads_and_truth(client, storage):
    resp = client.post("/demo/")
    assert resp.status_code == 302 and resp["Location"] == "/samples/DEMO001/"
    assert storage.exists("reference/synthetic_ref.fa")
    with storage.open_read("raw_reads/DEMO001.fastq.gz") as fh:
        assert gzip.decompress(fh.read()).startswith(b"@read0\n")
    with storage.open_read("truth/DEMO001.json") as fh:
        truth = json.load(fh)
    assert truth["sample"] == "DEMO001" and len(truth["snps"]) == demo.snp_count(1)


def test_demos_number_up_share_one_reference_and_differ(storage):
    assert demo.create_demo_sample(storage) == "DEMO001"
    with storage.open_read("reference/synthetic_ref.fa") as fh:
        ref1 = fh.read()
    assert demo.create_demo_sample(storage) == "DEMO002"
    with storage.open_read("reference/synthetic_ref.fa") as fh:
        assert fh.read() == ref1  # unchanged: every demo aligns to the same reference
    truths = []
    for sid in ("DEMO001", "DEMO002"):
        with storage.open_read(f"truth/{sid}.json") as fh:
            truths.append(json.load(fh)["snps"])
    assert truths[0] != truths[1]


def test_next_number_skips_gaps_and_other_samples(storage):
    put(storage, "raw_reads/DEMO007.fastq.gz", b"x")
    put(storage, "raw_reads/DEMOX.fastq.gz", b"x")
    put(storage, "raw_reads/SRR1.fastq.gz", b"x")
    assert demo.next_demo_number(storage) == 8


def test_refuses_to_overwrite_a_different_reference(client, storage):
    put(storage, "reference/synthetic_ref.fa", ">other\nACGT\n")
    with pytest.raises(demo.DemoError):
        demo.create_demo_sample(storage)
    assert not storage.exists("raw_reads/DEMO001.fastq.gz")
    resp = client.post("/demo/", follow=True)
    assert b"different contents" in resp.content
