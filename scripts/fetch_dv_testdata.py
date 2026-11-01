#!/usr/bin/env python3
"""Download DeepVariant's public quickstart slice (real data) into the local bucket.

NA12878 (GIAB HG001), Illumina paired-end, chr20:10,000,000-10,100,000 on hg19, with the GIAB
truth VCF and confident-region BED for that window. About 5 MB to download, ~64 MB on disk once
the reference is decompressed. Source: https://storage.googleapis.com/deepvariant/quickstart-testdata/

Written with the pipeline's key layout, so the DeepVariant stage can run on it directly:

    <root>/alignments/NA12878_S1.bam               (already aligned: run with --skip-to-variants)
    <root>/reference/ucsc.hg19.chr20.unittest.fasta
    <root>/truth/NA12878_S1.vcf.gz + .bed          (--check prints TP/FN/FP counts against these)

    python scripts/fetch_dv_testdata.py
    python scripts/run_local.py --sample NA12878_S1 --reference ucsc.hg19.chr20.unittest.fasta \\
        --caller deepvariant --skip-to-variants --check
"""

import argparse
import gzip
import os
import shutil
import tempfile
import urllib.request

BASE_URL = "https://storage.googleapis.com/deepvariant/quickstart-testdata/"
SAMPLE = "NA12878_S1"
REFERENCE = "ucsc.hg19.chr20.unittest.fasta"

# (remote name, expected size in bytes, local key, decompress?)
FILES = [
    ("NA12878_S1.chr20.10_10p1mb.bam", 3925783, f"alignments/{SAMPLE}.bam", False),
    ("ucsc.hg19.chr20.unittest.fasta.gz", 606944, f"reference/{REFERENCE}", True),
    ("test_nist.b37_chr20_100kbp_at_10mb.vcf.gz", 5898, f"truth/{SAMPLE}.vcf.gz", False),
    ("test_nist.b37_chr20_100kbp_at_10mb.bed", 264, f"truth/{SAMPLE}.bed", False),
]


def fetch(url, dest, expected_size):
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    size = os.path.getsize(dest)
    if size != expected_size:
        raise RuntimeError(f"{url}: got {size} bytes, expected {expected_size}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="local_bucket", help="storage root (LOCAL_STORAGE_DIR)")
    p.add_argument("--force", action="store_true", help="download again even if the files exist")
    a = p.parse_args(argv)

    total = sum(f[1] for f in FILES)
    print(f"Fetching {len(FILES)} files ({total / 1e6:.1f} MB) from {BASE_URL}")
    for name, size, key, decompress in FILES:
        dest = os.path.join(a.root, *key.split("/"))
        if os.path.exists(dest) and not a.force:
            print(f"  exists   {dest}")
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, name)
            fetch(BASE_URL + name, raw, size)
            if decompress:
                with gzip.open(raw, "rb") as src, open(dest + ".part", "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                os.replace(dest + ".part", dest)
            else:
                shutil.move(raw, dest)
        print(f"  fetched  {dest} ({os.path.getsize(dest):,} bytes)")
    print(f"Done. Sample {SAMPLE}, reference {REFERENCE}.")


if __name__ == "__main__":
    main()
