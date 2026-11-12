"""The large route's S3 paths, offline (#231): moto's S3 server as the endpoint, the pipeline image as the jobs.

Opt in with GEYSER_S3_E2E=1 after:  docker build -f Dockerfile -t geyser-app .   (GEYSER_IMAGE to use another tag)

The unit tests stub S3 and the Docker E2E reads local files, so these paths had only the cloud canary:
- presigned GETs: align_merge reads the chunk BAMs through presigned URLs (samtools, no S3 credentials);
- HTTP range reads: a calling shard reads only its region of the CRAM (bcftools with Range requests);
- the ETag-verified .crai: a shard streams only if the .crai's cram-etag metadata is the CRAM's current ETag, and
  falls back to download-and-index when the CRAM changed.

moto runs in this process (ThreadedMotoServer, pip, no image) on 0.0.0.0; the job containers reach it as
host.docker.internal (--add-host ...:host-gateway, so Linux CI works as Docker Desktop does). moto's request
recorder shows which requests the jobs made. It checks no signatures, so this covers S3 behaviour, not IAM (that is
the IAM policy simulator's job, #229).
"""

import gzip
import io
import json
import os
import socket
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_S3_E2E") != "1", reason="set GEYSER_S3_E2E=1 to run the S3 (moto) end-to-end test"
)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

BUCKET = "geyser-test"
REF = "synthetic_ref.fa"
LENGTH = 300_000  # bp: ~60,000 reads at 30x, several CRAM containers (10,000 records each), so a shard can seek
WINDOW = 50_000  # bp per calling shard: 6 regions
CHUNKS = 4


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def s3(tmp_path_factory):
    """A fresh moto S3 server, a client for it from the host, and the URL the job containers use."""
    # The request recorder appends to a file (default: ./moto_recording, kept across runs): one of our own instead.
    os.environ["MOTO_RECORDER_FILEPATH"] = str(tmp_path_factory.mktemp("moto") / "recording")
    import boto3
    import requests
    from moto.server import ThreadedMotoServer

    port = _free_port()
    server = ThreadedMotoServer(ip_address="0.0.0.0", port=port, verbose=False)
    server.start()
    host_url = f"http://127.0.0.1:{port}"
    client = boto3.client(
        "s3", endpoint_url=host_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    )
    client.create_bucket(Bucket=BUCKET)
    requests.post(f"{host_url}/moto-api/recorder/reset-recording").raise_for_status()

    class S3:
        url = host_url
        job_url = f"http://host.docker.internal:{port}"

        @staticmethod
        def record(on):
            requests.post(f"{host_url}/moto-api/recorder/reset-recording").raise_for_status()
            requests.post(f"{host_url}/moto-api/recorder/{'start' if on else 'stop'}-recording").raise_for_status()

        @staticmethod
        def recording():
            requests.post(f"{host_url}/moto-api/recorder/stop-recording").raise_for_status()
            text = requests.get(f"{host_url}/moto-api/recorder/download-recording").text
            return [json.loads(line) for line in text.splitlines() if line.strip()]

    S3.client = client
    yield S3
    server.stop()


def _job(s3, *cmd, run_name):
    """One pipeline job in the image, on S3 storage, as Batch would run it. Returns its combined output."""
    image = os.environ.get("GEYSER_IMAGE", "geyser-app")
    env = {
        "STORAGE_BACKEND": "s3",
        "BUCKET_NAME": BUCKET,
        "S3_ENDPOINT_URL": s3.job_url,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_REGION": "us-east-1",
        "METRICS_BACKEND": "none",
        "RUN_NAME": run_name,
    }
    full = ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway"]
    for k, v in env.items():
        full += ["-e", f"{k}={v}"]
    result = subprocess.run([*full, image, "python", "tasks.py", *cmd], capture_output=True, text=True)
    log = result.stdout + result.stderr
    assert result.returncode == 0, f"{' '.join(cmd)} failed:\n{log}"
    return log


def _upload_tree(s3, root, rename=None):
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            key = os.path.relpath(path, root).replace(os.sep, "/")
            if rename:
                key = rename(key)
            if key:
                s3.client.upload_file(path, BUCKET, key)


def _records(s3, key):
    body = s3.client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    with gzip.open(io.BytesIO(body), "rt") as fh:
        return [line for line in fh if not line.startswith("#")]


def _regions(s3, run):
    log = _job(s3, "regions", REF, "--window", str(WINDOW), run_name=run)
    return json.loads([line for line in log.splitlines() if line.startswith("[")][-1])


def _key_of(request):
    url = request["url"].split("?", 1)[0]
    return url.split(f"/{BUCKET}/", 1)[1] if f"/{BUCKET}/" in url else ""


def test_the_large_route_on_s3_streams_through_presigned_range_reads_and_matches_one_job(s3, tmp_path):
    import make_test_data

    root = tmp_path / "data"
    make_test_data.main(["--root", str(root), "--length", str(LENGTH)])
    _upload_tree(s3, root)
    # The same reads as a second sample for the small route (one align job, one unsharded variants job).
    s3.client.upload_file(str(root / "raw_reads" / "SYNTH001.fastq.gz"), BUCKET, "raw_reads/SINGLE001.fastq.gz")

    _job(s3, "align", "SINGLE001", REF, run_name="SINGLE001-small")
    _job(s3, "variants", "SINGLE001", REF, run_name="SINGLE001-small")

    # The large route: pre-flight into chunks, align each, merge (markdup) into one CRAM.
    run = "SYNTH001-large"
    log = _job(s3, "preflight", "SYNTH001", "--chunks", str(CHUNKS), run_name=run)
    assert "Pre-flight OK" in log
    for i in range(CHUNKS):
        _job(s3, "align_chunk", "SYNTH001", REF, "--chunk", str(i), run_name=run)
    s3.record(True)
    log = _job(s3, "align_merge", "SYNTH001", REF, run_name=run)
    merge_requests = s3.recording()
    assert f"Merging {CHUNKS} chunk BAMs" in log
    # Presigned GETs: samtools read every chunk BAM through a signed URL (query-string auth, no credentials).
    chunk_gets = [
        r for r in merge_requests if r["method"] == "GET" and _key_of(r).endswith(".bam") and "Signature=" in r["url"]
    ]
    assert len({_key_of(r) for r in chunk_gets}) == CHUNKS, [_key_of(r) for r in merge_requests]

    # Region-sharded calling, then the merge: the shards read their region of the CRAM in place.
    regions = _regions(s3, run)
    assert len(regions) >= 3
    s3.record(True)
    for r in regions:
        log = _job(s3, "variants", "SYNTH001", REF, "--region", r, run_name=run)
        assert "Read the region in place (ETag-verified index)" in log, log
    shard_requests = s3.recording()
    _job(s3, "variants", "SYNTH001", REF, "--window", str(WINDOW), run_name=run)
    cram = [r for r in shard_requests if _key_of(r) == "alignments/SYNTH001.cram" and r["method"] == "GET"]
    assert cram and all("Signature=" in r["url"] for r in cram)  # through presigned URLs, as htslib can't sign
    # htslib opens the CRAM at offset 0 (its header) and then seeks: a seek past what it has buffered is a new GET
    # with a Range from that offset (a region inside the first buffer needs none: 4 of 6 shards seek here).
    starts = {int(r["headers"]["Range"].split("=")[1].split("-")[0]) for r in cram if "Range" in r["headers"]}
    assert len({s for s in starts if s > 0}) >= len(regions) // 2, starts

    # The VCF: the same records as the single-job route (the sample name is only in the header).
    assert _records(s3, "variants/SYNTH001.vcf.gz") == _records(s3, "variants/SINGLE001.vcf.gz")


def test_a_shard_falls_back_to_download_when_the_cram_changed(s3, tmp_path):
    # Runs after the large route above (same module, same server). Replace the CRAM with another sample's (the same
    # reads, a different read group: other bytes, so another ETag); the stored .crai's cram-etag no longer matches.
    run = "SYNTH001-swapped"
    before = s3.client.head_object(Bucket=BUCKET, Key="alignments/SYNTH001.cram")["ETag"]
    s3.client.copy_object(
        Bucket=BUCKET, Key="alignments/SYNTH001.cram", CopySource={"Bucket": BUCKET, "Key": "alignments/SINGLE001.cram"}
    )
    assert s3.client.head_object(Bucket=BUCKET, Key="alignments/SYNTH001.cram")["ETag"] != before
    regions = _regions(s3, run)
    log = _job(s3, "variants", "SYNTH001", REF, "--region", regions[0], run_name=run)
    assert "The stored index isn't recorded as this CRAM's: downloading the CRAM" in log, log
    assert "Read the region in place" not in log
    # Its calls are still right: the same records as the unsharded small route's, in that region.
    parts = s3.client.list_objects_v2(Bucket=BUCKET, Prefix=f"intermediate/variant-parts/SYNTH001/{run}/")["Contents"]
    part = [o["Key"] for o in parts if o["Key"].endswith(".vcf.gz")]
    assert len(part) == 1
    contig, span = regions[0].split(":")
    start, end = (int(x) for x in span.split("-"))
    want = [
        line
        for line in _records(s3, "variants/SINGLE001.vcf.gz")
        if line.split("\t")[0] == contig and start <= int(line.split("\t")[1]) <= end
    ]
    assert _records(s3, part[0]) == want
