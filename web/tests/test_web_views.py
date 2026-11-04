import json

from samples.runs import write_run

from .web_helpers import FASTP_SINGLE, fastq_gz, metric, put, vcf_gz, write_metrics

FINISHED = [
    "qc_reports/S1.fastp.html",
    "alignments/S1.cram",
    "alignments/S1.cram.crai",
    "variants/S1.vcf.gz.tbi",
    "stats/S1/flagstat.txt",
    "stats/S1/mosdepth.summary.txt",
    "stats/S1/mosdepth.global.dist.txt",
    "stats/S1/bcftools.stats.txt",
]


def finished_sample(storage, truth=True):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    for key in FINISHED:
        put(storage, key, b"<html><body>fastp report</body></html>" if key.endswith(".html") else b"data")
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G"), (300, "C", "T"), (500, "G", "A")]))
    write_metrics(
        storage,
        [
            metric("QualityControl", "S1", duration=7.2),
            metric("Align", "S1", duration=2.5),
            metric("CallVariants", "S1", duration=1.1),
        ],
    )
    if truth:
        put(
            storage,
            "truth/S1.json",
            json.dumps(
                {
                    "snps": [
                        {"pos": 100, "ref": "A", "alt": "G"},
                        {"pos": 300, "ref": "C", "alt": "T"},
                        {"pos": 900, "ref": "T", "alt": "C"},
                    ]
                }
            ),
        )


def test_empty_list_page(client, storage):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No samples yet" in resp.content
    assert b"csrfmiddlewaretoken" in resp.content
    assert b"data-poll" not in resp.content  # nothing to poll


def test_list_shows_samples_and_polls_only_while_active(client, storage):
    finished_sample(storage)
    put(storage, "raw_reads/S2.fastq.gz", fastq_gz())
    write_run(storage, "S2", {"state": "queued", "queued": 1})
    resp = client.get("/")
    body = resp.content.decode()
    assert "/samples/S1/" in body and "/samples/S2/" in body
    assert "Complete" in body and "Queued" in body
    assert 'data-poll="/samples/rows/"' in body and 'data-poll-seconds="' in body

    write_run(storage, "S2", {"state": "failed", "queued": 1, "started": 2, "finished": 3, "exit_code": 1})
    rows = client.get("/samples/rows/").content.decode()
    assert "Failed" in rows and "data-poll" not in rows  # finished: geyser.js stops polling


def test_detail_page_shows_variants_truth_qc_and_downloads(client, storage):
    finished_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "2/3 planted SNPs recovered" in body
    assert "1 unexpected call" in body
    assert body.count('class="planted"') == 2
    assert "Variants" in body and "3 calls" in body
    assert (
        'href="/samples/S1/files/qc_html/?inline=1" target="_blank"' in body and "<iframe" not in body
    )  # on request only
    assert "Bases ≥ Q30" in body and "93.4%" in body and "Duplication (fastp estimate)" in body
    for kind in (
        "raw",
        "qc_html",
        "qc_json",
        "cram",
        "crai",
        "vcf",
        "tbi",
        "truth",
        "flagstat",
        "mosdepth_summary",
        "mosdepth_dist",
        "bcftools_stats",
    ):
        assert f"/samples/S1/files/{kind}/" in body
    assert "/samples/S1/files/bam/" not in body  # only offered when a BAM exists (older runs)
    assert "/samples/S1/files/log/" not in body  # no run log for this sample
    assert "10.8 s" in body  # total of the stage durations


def test_detail_page_for_running_sample_polls_and_shows_live_log(client, storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_run(storage, "S1", {"state": "running", "started": 1})
    put(storage, "runs/S1.log", ">>> python tasks.py qc S1\n")
    body = client.get("/samples/S1/").content.decode()
    assert "?was_active=1" in body
    assert "python tasks.py qc S1" in body
    assert "Variants" not in body


def test_status_partial_asks_for_reload_when_run_finishes(client, storage):
    finished_sample(storage)
    resp = client.get("/samples/S1/status/?was_active=1")
    assert resp.status_code == 204 and resp["Geyser-Refresh"] == "true"  # geyser.js reloads the page
    resp = client.get("/samples/S1/status/")
    assert resp.status_code == 200 and b"run-status" in resp.content


def test_unknown_or_invalid_sample_is_404(client, storage):
    assert client.get("/samples/NOPE/").status_code == 404
    assert client.get("/samples/a.b/").status_code == 404
    assert client.get("/samples/NOPE/status/").status_code == 404


def test_downloads_are_whitelisted(client, storage):
    finished_sample(storage)
    resp = client.get("/samples/S1/files/cram/")
    assert resp.status_code == 200
    assert resp["Content-Disposition"].startswith('attachment; filename="S1.cram"')
    assert b"".join(resp.streaming_content) == b"data"
    assert client.get("/samples/S1/files/metrics/").status_code == 404
    assert client.get("/samples/S1/files/..%2F..%2Fetc/").status_code == 404
    assert client.get("/samples/..%2Fraw_reads/files/raw/").status_code == 404
    assert client.get("/samples/S1/files/log/").status_code == 404  # allowed kind, but doesn't exist


def test_qc_report_is_sandboxed_and_frameable_only_by_us(client, storage):
    finished_sample(storage)
    resp = client.get("/samples/S1/files/qc_html/?inline=1")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    assert resp["Content-Security-Policy"] == "sandbox"
    assert resp["X-Frame-Options"] == "SAMEORIGIN"
    assert client.get("/")["X-Frame-Options"] == "DENY"


def test_list_handles_failure_metric(client, storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_metrics(storage, [metric("QualityControl", "S1", "Failure")])
    body = client.get("/").content.decode()
    assert "Failed" in body


def test_a_bam_from_before_cram_is_still_downloadable(client, storage):
    finished_sample(storage)
    put(storage, "alignments/S1.bam", b"old")
    assert "/samples/S1/files/bam/" in client.get("/samples/S1/").content.decode()
    resp = client.get("/samples/S1/files/bam/")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == b"old"


CSQ_VCF = (
    "##fileformat=VCFv4.2\n"
    '##INFO=<ID=BCSQ,Number=.,Type=String,Description="Haplotype-aware consequence annotation from BCFtools/csq">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
    "synth1\t100\t.\tA\tG\t225\t.\tDP=30;BCSQ=missense|SYNTHG1|SYNTHG1T|protein_coding|+|5N>5S|100A>G\tGT\t1/1\n"
    "synth1\t300\t.\tC\tT\t225\t.\tDP=30;BCSQ=*synonymous|SYNTHG2|SYNTHG2T|protein_coding|-|9Y|300C>T\tGT\t1/1\n"
    "synth1\t500\t.\tG\tA\t225\t.\tDP=30\tGT\t1/1\n"
)


def test_detail_page_shows_consequences_from_the_annotated_vcf(client, storage):
    import gzip

    finished_sample(storage)
    put(storage, "variants/S1.csq.vcf.gz", gzip.compress(CSQ_VCF.encode()))
    body = client.get("/samples/S1/").content.decode()
    assert "<th>Consequence</th>" in body
    assert "missense SYNTHG1" in body and "synonymous SYNTHG2" in body  # the '*' marker is dropped
    assert "/samples/S1/files/csq_vcf/" in body and "Annotated variants" in body
    assert "2/3 planted SNPs recovered" in body  # same records, same truth check


def test_detail_page_without_annotation_has_no_consequence_column(client, storage):
    finished_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "Consequence" not in body and "/samples/S1/files/csq_vcf/" not in body


def test_an_annotated_vcf_from_an_earlier_run_is_not_shown_or_offered(client, storage):
    # A later variants run that didn't annotate leaves the old csq VCF in place (nothing deletes it);
    # the variants stage's part says what its last run wrote.
    import gzip

    finished_sample(storage)
    put(storage, "variants/S1.csq.vcf.gz", gzip.compress(CSQ_VCF.encode()))
    part = {"task": "CallVariants", "run_name": "S1-run2", "outputs": [{"key": "variants/S1.vcf.gz", "bytes": 1}]}
    put(storage, "runs/S1.provenance/CallVariants.json", json.dumps(part))
    body = client.get("/samples/S1/").content.decode()
    assert "<th>Consequence</th>" not in body and "/samples/S1/files/csq_vcf/" not in body

    part["outputs"].append({"key": "variants/S1.csq.vcf.gz", "bytes": 1})
    put(storage, "runs/S1.provenance/CallVariants.json", json.dumps(part))
    body = client.get("/samples/S1/").content.decode()
    assert "<th>Consequence</th>" in body and "/samples/S1/files/csq_vcf/" in body


def test_a_later_run_that_stopped_before_variants_keeps_the_annotation(client, storage):
    # run2 failed in align: its parts don't include CallVariants, whose part is still run1's.
    import gzip

    finished_sample(storage)
    put(storage, "variants/S1.csq.vcf.gz", gzip.compress(CSQ_VCF.encode()))
    run1 = {
        "task": "CallVariants",
        "run_name": "S1-run1",
        "finished": "2026-11-02T10:00:00Z",
        "outputs": [{"key": "variants/S1.csq.vcf.gz", "bytes": 1}, {"key": "variants/S1.vcf.gz", "bytes": 1}],
    }
    run2 = {
        "task": "Align",
        "run_name": "S1-run2",
        "finished": "2026-11-02T11:00:00Z",
        "status": "Failure",
        "outputs": [],
    }
    put(storage, "runs/S1.provenance/CallVariants.json", json.dumps(run1))
    put(storage, "runs/S1.provenance/Align.json", json.dumps(run2))
    assert "<th>Consequence</th>" in client.get("/samples/S1/").content.decode()


# --- DeepVariant as the run's caller (#125 follow-up) -----------------------------------------------

DV_VCF_LINES = [
    (100, "A", "G", "PASS"),
    (300, "C", "T", "PASS"),
    (500, "G", "A", "PASS"),
    (700, "T", "C", "RefCall"),  # a rejected candidate: not a call
]


def dv_vcf_gz():
    from .web_helpers import VCF_HEADER, gz

    lines = "".join(f"synth1\t{p}\t.\t{r}\t{a}\t30\t{f}\t.\tGT\t0/1\n" for p, r, a, f in DV_VCF_LINES)
    return gz((VCF_HEADER + lines).encode())


def dv_parts(storage, dv_status="Success", fallback=False):
    import provenance

    stages = [
        ("QualityControl", "Success", None),
        ("Align", "Success", None),
        ("DeepVariant", dv_status, "deepvariant"),
    ]
    if fallback:
        stages.append(("CallVariants", "Success", None))
    for i, (task, status, caller) in enumerate(stages):
        at = f"2026-11-02T10:0{i}:00Z"
        part = {
            "task": task,
            "status": status,
            "run_name": "R1",
            "started": at,
            "finished": at,
            "inputs": [],
            "outputs": [],
            "tools": {},
            "stats": {},
        }
        if caller:
            part["caller"] = caller
        provenance._put_json(storage, f"runs/S1.provenance/{task}.json", part)


def dv_sample(storage, dv_vcf=True):
    """A cloud run with caller=deepvariant: QC and align, then DeepVariant's VCF; no bcftools VCF."""
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    for key in ("qc_reports/S1.fastp.html", "alignments/S1.cram", "alignments/S1.cram.crai"):
        put(storage, key, b"data")
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    if dv_vcf:
        put(storage, "variants_deepvariant/S1.vcf.gz", dv_vcf_gz())
    put(
        storage,
        "truth/S1.json",
        json.dumps({"snps": [{"pos": p, "ref": r, "alt": a} for p, r, a, _ in DV_VCF_LINES[:3]]}),
    )
    dv_parts(storage)


def test_a_deepvariant_run_shows_deepvariants_calls_and_is_complete(client, storage):
    dv_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "3/3 planted SNPs recovered" in body and "0 unexpected calls" in body  # RefCall isn't a call
    assert "Complete" in body
    assert "/samples/S1/files/vcf/" in body and "/samples/S1/files/tbi/" not in body
    response = client.get("/samples/S1/files/vcf/")
    assert response.status_code == 200 and b"".join(response.streaming_content) == dv_vcf_gz()
    assert response["Content-Disposition"].endswith('S1.vcf.gz"')
    assert client.get("/samples/S1/files/tbi/").status_code == 404
    listing = client.get("/").content.decode()
    assert "S1" in listing and "Complete" in listing


def test_a_stale_bcftools_index_is_not_offered_for_deepvariants_calls(client, storage):
    dv_sample(storage)
    put(storage, "variants/S1.vcf.gz.tbi", b"old index")  # left by an earlier bcftools run
    body = client.get("/samples/S1/").content.decode()
    assert "/samples/S1/files/tbi/" not in body


def test_after_the_bcftools_fallback_the_page_shows_bcftools_calls(client, storage):
    dv_sample(storage)
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G")]))
    dv_parts(storage, dv_status="Failure", fallback=True)
    body = client.get("/samples/S1/").content.decode()
    assert "1/3 planted SNPs recovered" in body
    assert b"".join(client.get("/samples/S1/files/vcf/").streaming_content) == vcf_gz([(100, "A", "G")])


def test_the_list_reads_the_record_even_with_an_old_bcftools_vcf(client, storage):
    # bcftools ran earlier; this run's caller is DeepVariant and it failed (no local fallback): not Complete.
    dv_sample(storage, dv_vcf=False)
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G")]))
    dv_parts(storage, dv_status="Failure")
    listing = client.get("/").content.decode()
    assert "Complete" not in listing
