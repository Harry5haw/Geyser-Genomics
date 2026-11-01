#!/usr/bin/env python3
"""Download public real-data test sets for the DeepVariant stage into the local bucket.

Datasets (all from DeepVariant's public bucket, https://storage.googleapis.com/deepvariant/):

  na12878-slice (default)  NA12878 (GIAB HG001), Illumina, chr20:10.0-10.1 Mb on hg19, with GIAB truth.
                           ~5 MB download, ~64 MB on disk. Sample NA12878_S1.
  hg003-chr20              HG003, NovaSeq PCR-free 35x, all of chr20 on GRCh38 (DeepVariant's WGS case
                           study), with GIAB v4.2.1 truth cut down to chr20. ~1.34 GB download
                           (1.11 GB BAM; only chr20's bytes of the 3.1 GB reference are fetched, by HTTP
                           range; the genome-wide truth VCF/BED are filtered to chr20 locally).
                           ~1.2 GB on disk. Sample HG003_chr20.

Written with the pipeline's key layout, so the DeepVariant stage runs on it directly:

    <root>/alignments/<sample>.bam          (already aligned: run with --skip-to-variants)
    <root>/reference/<reference>
    <root>/truth/<sample>.vcf.gz + .bed     (--check prints TP/FN/FP counts against these)

    python scripts/fetch_dv_testdata.py [--dataset hg003-chr20]
    python scripts/run_local.py --sample <sample> --reference <reference> \\
        --caller deepvariant --skip-to-variants --check

--pangenome also gets the HPRC v1.1 Minigraph-Cactus pangenome graph on GRCh38 (the graph Google's
pangenome-aware case studies use), from the Human Pangenome Reference Consortium's public bucket, for
pangenome-aware DeepVariant (run_local.py --caller pangenome). Only valid with GRCh38 datasets (hg003-chr20).

  --pangenome / --pangenome chr20  HPRC's chr20 graph (chroms/chr20.vg, 1.28 GB download), converted with vg
                                   (Docker, quay.io/vgteam/vg) to <root>/pangenome/hprc-v1.1-mc-grch38.chr20.gbz
                                   (72 MB; same 46 samples, 90 haplotypes and GRCh38/CHM13 references).
  --pangenome full                 the whole-genome graph as published (3.29 GB) to
                                   <root>/pangenome/hprc-v1.1-mc-grch38.gbz. Loaded, it needs ~10.7 GB of shared
                                   memory plus ~2 GB per make_examples worker.
"""

import argparse
import gzip
import os
import shutil
import subprocess
import urllib.request

BASE_URL = "https://storage.googleapis.com/deepvariant/"
GRCH38 = "case-study-testdata/GCA_000001405.15_GRCh38_no_alt_analysis_set.fa"

# Pangenome graphs for pangenome-aware DeepVariant. Remote paths that are full URLs are fetched as-is (not under
# BASE_URL). "build", when present, is the GBZ made from the download by build_chr20_gbz().
HPRC_GRCH38 = ("https://s3-us-west-2.amazonaws.com/human-pangenomics/pangenomes/freeze/freeze1/minigraph-cactus/"
               "hprc-v1.1-mc-grch38/")
PANGENOMES = {
    "chr20": {
        "url": HPRC_GRCH38 + "hprc-v1.1-mc-grch38.chroms/chr20.vg",
        "size": 1279054691,
        "key": "pangenome/chroms/chr20.vg",
        "build": "pangenome/hprc-v1.1-mc-grch38.chr20.gbz",
    },
    "full": {
        "url": HPRC_GRCH38 + "hprc-v1.1-mc-grch38.gbz",
        "size": 3289083600,
        "key": "pangenome/hprc-v1.1-mc-grch38.gbz",
    },
}
GRCH38_DATASETS = {"hg003-chr20"}
# vg release used by Google's pangenome-aware VG case study.
VG_IMAGE = "quay.io/vgteam/vg:v1.55.0"
# The whole-genome graph marks both references; keep them as references in the chr20 graph too.
REFERENCE_SAMPLES = ("GRCh38", "CHM13")

# Each file: (remote path, expected bytes or None, local key, how to store it)
#   copy        store as downloaded
#   gunzip      decompress
#   fasta:<ctg> fetch only that contig from an uncompressed FASTA, by HTTP range (size from its .fai)
#   only:<ctg>  keep only that contig's rows (VCF keeps its header; output re-gzipped if .gz)
DATASETS = {
    "na12878-slice": {
        "sample": "NA12878_S1",
        "reference": "ucsc.hg19.chr20.unittest.fasta",
        "files": [
            ("quickstart-testdata/NA12878_S1.chr20.10_10p1mb.bam", 3925783, "alignments/NA12878_S1.bam", "copy"),
            ("quickstart-testdata/ucsc.hg19.chr20.unittest.fasta.gz", 606944,
             "reference/ucsc.hg19.chr20.unittest.fasta", "gunzip"),
            ("quickstart-testdata/test_nist.b37_chr20_100kbp_at_10mb.vcf.gz", 5898, "truth/NA12878_S1.vcf.gz", "copy"),
            ("quickstart-testdata/test_nist.b37_chr20_100kbp_at_10mb.bed", 264, "truth/NA12878_S1.bed", "copy"),
        ],
    },
    "hg003-chr20": {
        "sample": "HG003_chr20",
        "reference": "GRCh38_chr20.fa",
        "files": [
            ("case-study-testdata/HG003.novaseq.pcr-free.35x.dedup.grch38_no_alt.chr20.bam", 1112015824,
             "alignments/HG003_chr20.bam", "copy"),
            (GRCH38, None, "reference/GRCh38_chr20.fa", "fasta:chr20"),
            ("case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark.vcf.gz", 147212428,
             "truth/HG003_chr20.vcf.gz", "only:chr20"),
            ("case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed", 13219856,
             "truth/HG003_chr20.bed", "only:chr20"),
        ],
    },
}


def download(url, dest, expected_size=None, byte_range=None):
    """Stream url to dest, printing progress for large files, and check the size."""
    req = urllib.request.Request(url)
    if byte_range:
        req.add_header("Range", f"bytes={byte_range[0]}-{byte_range[1]}")
        expected_size = byte_range[1] - byte_range[0] + 1
    done, next_report = 0, 100_000_000
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done >= next_report:
                print(f"    {done / 1e6:,.0f} MB" + (f" / {expected_size / 1e6:,.0f} MB" if expected_size else ""),
                      flush=True)
                next_report += 100_000_000
    if expected_size is not None and done != expected_size:
        raise RuntimeError(f"{url}: got {done} bytes, expected {expected_size}")


def fasta_contig_range(fai_text, contig):
    """(first byte, last byte, length) of a contig's sequence in a FASTA, from its .fai."""
    for line in fai_text.splitlines():
        name, length, offset, line_bases, line_width = line.split("\t")[:5]
        if name == contig:
            length, offset, line_bases, line_width = int(length), int(offset), int(line_bases), int(line_width)
            full, rest = divmod(length, line_bases)
            nbytes = full * line_width + (rest + line_width - line_bases if rest else 0)
            return offset, offset + nbytes - 1, length
    raise ValueError(f"{contig} not in index")


def fetch_fasta_contig(url, contig, dest):
    with urllib.request.urlopen(url + ".fai", timeout=120) as resp:
        first, last, length = fasta_contig_range(resp.read().decode("ascii"), contig)
    body = dest + ".body"
    download(url, body, byte_range=(first, last))
    with open(dest + ".part", "wb") as out, open(body, "rb") as src:
        out.write(f">{contig}\n".encode("ascii"))
        shutil.copyfileobj(src, out, length=1024 * 1024)
    os.remove(body)
    bases = 0
    with open(dest + ".part", "rb") as fh:
        next(fh)
        for line in fh:
            bases += len(line.rstrip(b"\r\n"))
    if bases != length:
        raise RuntimeError(f"{contig}: got {bases} bases, expected {length}")
    os.replace(dest + ".part", dest)


def keep_contig(src, dest, contig):
    """Copy src to dest keeping header lines and rows on one contig. Returns rows kept."""
    opener = gzip.open if src.endswith(".gz") else open
    prefix, kept = contig + "\t", 0
    # newline="" keeps line endings byte-for-byte (no \r\n on Windows)
    with opener(src, "rt", encoding="utf-8", newline="") as fin, \
            opener(dest + ".part", "wt", encoding="utf-8", newline="") as fout:
        for line in fin:
            if line.startswith("#") or line.startswith(prefix):
                fout.write(line)
                kept += not line.startswith("#")
    os.replace(dest + ".part", dest)
    return kept


def remote_url(remote):
    """Full URL for a file entry: absolute URLs as-is, anything else under DeepVariant's bucket."""
    return remote if remote.startswith(("https://", "http://")) else BASE_URL + remote


def dataset_files(name, pangenome=None):
    """The file entries to fetch for a dataset, plus a pangenome graph ('chr20' or 'full') when asked.

    Pangenome graphs are on GRCh38, so only GRCh38 datasets accept one.
    """
    files = list(DATASETS[name]["files"])
    if pangenome:
        if name not in GRCH38_DATASETS:
            raise ValueError(f"--pangenome needs a GRCh38 dataset ({', '.join(sorted(GRCH38_DATASETS))}); "
                             f"{name} is on another reference")
        graph = PANGENOMES[pangenome]
        files.append((graph["url"], graph["size"], graph["key"], "copy"))
    return files


def vg_build_command(root, vg_key, gbz_key, memory="8g"):
    """docker run that converts a .vg graph to GBZ (via GFA) with the HPRC reference samples kept as references.

    vg convert -f writes the haplotypes as GFA walks; vg gbwt -G indexes them and stores graph + index as GBZ.
    The GFA is a temporary file (about 1 GB for chr20) and is removed afterwards.
    """
    vg, gbz = f"/data/{vg_key}", f"/data/{gbz_key}"
    gfa = gbz + ".gfa"
    refs = " ".join(f"--set-reference {s}" for s in REFERENCE_SAMPLES)
    script = (f"set -e; vg convert -f {vg} > {gfa}; "
              f"vg gbwt -G {gfa} --gbz-format -g {gbz}.part {refs}; mv {gbz}.part {gbz}; rm -f {gfa}")
    return ["docker", "run", "--rm", "--memory", memory,
            "--mount", f"type=bind,source={os.path.abspath(root)},target=/data",
            VG_IMAGE, "bash", "-c", script]


def build_chr20_gbz(root, force=False):
    graph = PANGENOMES["chr20"]
    dest = os.path.join(root, *graph["build"].split("/"))
    if os.path.exists(dest) and not force:
        print(f"  exists   {dest}")
        return
    print(f"  building {graph['build']} with {VG_IMAGE} (about 4 minutes, under 3 GB of memory)", flush=True)
    subprocess.run(vg_build_command(root, graph["key"], graph["build"]), check=True)
    print(f"  wrote    {dest} ({os.path.getsize(dest):,} bytes)", flush=True)


def fetch_dataset(name, root, force=False, pangenome=None):
    ds = DATASETS[name]
    files = dataset_files(name, pangenome)
    known = sum(f[1] or 0 for f in files)
    print(f"Dataset {name}: {len(files)} files, {known / 1e6:,.1f} MB plus any contig ranges")
    for remote, size, key, how in files:
        dest = os.path.join(root, *key.split("/"))
        if os.path.exists(dest) and not force:
            print(f"  exists   {dest}")
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        url = remote_url(remote)
        print(f"  fetching {remote}", flush=True)
        if how.startswith("fasta:"):
            fetch_fasta_contig(url, how.split(":", 1)[1], dest)
        else:
            raw = dest + ".download"
            download(url, raw, size)
            if how == "copy":
                os.replace(raw, dest)
            elif how == "gunzip":
                with gzip.open(raw, "rb") as src, open(dest + ".part", "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                os.replace(dest + ".part", dest)
                os.remove(raw)
            elif how.startswith("only:"):
                # keep the .gz suffix on the temp name so keep_contig reads it as gzip
                tmp = raw + (".gz" if remote.endswith(".gz") else "")
                os.replace(raw, tmp)
                rows = keep_contig(tmp, dest, how.split(":", 1)[1])
                os.remove(tmp)
                print(f"    kept {rows:,} rows on {how.split(':', 1)[1]}")
            else:
                raise ValueError(how)
        print(f"  wrote    {dest} ({os.path.getsize(dest):,} bytes)", flush=True)
    if pangenome and "build" in PANGENOMES[pangenome]:
        build_chr20_gbz(root, force)
    print(f"Done. Sample {ds['sample']}, reference {ds['reference']}.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="local_bucket", help="storage root (LOCAL_STORAGE_DIR)")
    p.add_argument("--dataset", choices=sorted(DATASETS), default="na12878-slice")
    p.add_argument("--force", action="store_true", help="download again even if the files exist")
    p.add_argument("--pangenome", nargs="?", const="chr20", choices=sorted(PANGENOMES),
                   help="also get the HPRC v1.1 GRCh38 pangenome graph for --caller pangenome: chr20 (default; "
                        "built locally with vg, 72 MB) or full (3.29 GB, needs ~11 GB of shared memory)")
    a = p.parse_args(argv)
    try:
        fetch_dataset(a.dataset, a.root, a.force, a.pangenome)
    except ValueError as err:
        p.error(str(err))


if __name__ == "__main__":
    main()
