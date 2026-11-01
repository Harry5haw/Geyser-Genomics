"""Helpers shared by the web tests (a plain module, so test files can import them)."""

import gzip
import io
import json


def put(storage, key, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    storage.upload_stream(io.BytesIO(data), key)


def gz(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)  # deterministic bytes (the header holds a timestamp)


def fastq_gz(reads=2) -> bytes:
    return gz("".join(f"@r{i}\nACGTACGT\n+\nIIIIIIII\n" for i in range(reads)).encode())


VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
)


def vcf_gz(records) -> bytes:
    """records: (pos, ref, alt[, qual, dp, gt])"""
    lines = []
    for rec in records:
        pos, ref, alt = rec[:3]
        qual, dp, gt = (list(rec[3:]) + [225.4, 30, "1/1"][len(rec) - 3:])[:3]
        lines.append(f"synth1\t{pos}\t.\t{ref}\t{alt}\t{qual}\t.\tDP={dp};MQ=60\tGT:PL\t{gt}:255,90,0\n")
    return gz((VCF_HEADER + "".join(lines)).encode())


def metric(task, sample_id, status="Success", duration=1.5, timestamp=1000.0):
    return {"timestamp": timestamp, "task": task, "sample_id": sample_id, "status": status,
            "duration_seconds": duration}


def write_metrics(storage, records):
    put(storage, "metrics/metrics.jsonl", "".join(json.dumps(r) + "\n" for r in records))
