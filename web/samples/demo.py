"""'Generate demo sample': synthetic reads with known SNPs, written through the storage layer.

scripts/make_test_data.py writes to a folder, so it runs in a temp dir and the files are then
uploaded with the normal storage calls. That keeps the UI storage-only and works against S3 too.

Every demo uses the same reference (same seed and length, so identical bytes). The SNP count
varies with the demo number, which changes where the SNPs land, so each demo looks different.
"""

import contextlib
import io
import os
import re
import tempfile

from geyser_web.pipeline import all_raw_keys, keys_for, make_test_data

from .runs import put_bytes
from .vcf import truth_key

DEMO_PREFIX = "DEMO"
DEMO_REFERENCE = "synthetic_ref.fa"
_DEMO_RE = re.compile(rf"^raw_reads/{DEMO_PREFIX}(\d+)\.fastq\.gz$")


class DemoError(Exception):
    pass


def demo_id(number: int) -> str:
    return f"{DEMO_PREFIX}{number:03d}"


def next_demo_number(storage) -> int:
    numbers = [int(m.group(1)) for k in storage.list_keys(f"raw_reads/{DEMO_PREFIX}") if (m := _DEMO_RE.match(k))]
    return max(numbers, default=0) + 1


def snp_count(number: int) -> int:
    return 8 + (number - 1) % 7  # 8..14


def create_demo_sample(storage) -> str:
    """Generate the next DEMO### sample and return its ID."""
    number = next_demo_number(storage)
    # someone else just took it, in either layout
    while any(storage.exists(k) for k in all_raw_keys(demo_id(number))):
        number += 1
    sample_id = demo_id(number)

    with tempfile.TemporaryDirectory(prefix="geyser-demo-") as tmp:
        with contextlib.redirect_stdout(io.StringIO()):
            make_test_data.main([
                "--root", tmp, "--sample", sample_id, "--reference", DEMO_REFERENCE,
                "--snps", str(snp_count(number)),
            ])
        ref_key = f"reference/{DEMO_REFERENCE}"
        local_ref = os.path.join(tmp, "reference", DEMO_REFERENCE)
        with open(local_ref, "rb") as fh:
            ref_bytes = fh.read()
        if storage.exists(ref_key):
            with storage.open_read(ref_key) as fh:
                if fh.read() != ref_bytes:
                    raise DemoError(f"{ref_key} already exists with different contents; "
                                    "remove it or regenerate it with scripts/make_test_data.py defaults.")
        else:
            put_bytes(storage, ref_key, ref_bytes)

        with open(os.path.join(tmp, "truth", f"{sample_id}.json"), "rb") as fh:
            put_bytes(storage, truth_key(sample_id), fh.read())
        # The raw reads go last: their arrival is what triggers a run.
        storage.upload(os.path.join(tmp, "raw_reads", f"{sample_id}.fastq.gz"), keys_for(sample_id)["raw"])
    return sample_id
