#!/usr/bin/env python3
"""Download public real-data test sets for the DeepVariant stage into the local bucket.

Datasets (from DeepVariant's public bucket, https://storage.googleapis.com/deepvariant/, unless noted):

  na12878-slice (default)  NA12878 (GIAB HG001), Illumina, chr20:10.0-10.1 Mb on hg19, with GIAB truth.
                           ~5 MB download, ~64 MB on disk. Sample NA12878_S1.
  hg003-chr20              HG003, NovaSeq PCR-free 35x, all of chr20 on GRCh38 (DeepVariant's WGS case
                           study), with GIAB v4.2.1 truth cut down to chr20. ~1.34 GB download
                           (1.11 GB BAM; only chr20's bytes of the 3.1 GB reference are fetched, by HTTP
                           range; the genome-wide truth VCF/BED are filtered to chr20 locally).
                           ~1.2 GB on disk. Sample HG003_chr20.
  hg005-chr20              HG005 (GIAB, Han Chinese), NovaSeq PCR-free 30x, chr20 on GRCh38, with GIAB v4.2.1
                           truth cut down to chr20. Sample HG005_chr20. The BAM is from Google's
                           brain-genomics-public bucket (BWA-MEM 0.7.17 to hs38DH, Picard MarkDuplicates); only
                           chr20 is fetched, by samtools over HTTP in the geyser-app image (~0.9 GB). GIAB publishes
                           no "noinconsistent" BED for HG005, so the plain v4.2.1 BED is used. Truth from GIAB's FTP.
  hg003-30x-chr20          HG003 from the same 30x set and pipeline as hg005-chr20, with the same truth as
                           hg003-chr20. Sample HG003_30x_chr20. The control for HG005: same sequencing, coverage,
                           aligner and reference, so ancestry is what differs.
  hg003-chr20-vg           The same HG003 35x reads as hg003-chr20, aligned by Google with vg giraffe 1.55.0 to the
                           whole-genome HPRC v1.1 graph (haplotype sampling, surjected to GRCh38), then cut to chr20
                           (DeepVariant's pangenome-aware VG case study). 947 MB BAM, same truth as hg003-chr20.
                           Sample HG003_chr20_vg. Unlike the BWA BAM it has no duplicate marking, as published.

Written with the pipeline's key layout, so the DeepVariant stage runs on it directly:

    <root>/alignments/<sample>.bam          (already aligned: run with --skip-to-variants)
    <root>/reference/<reference>
    <root>/truth/<sample>.vcf.gz + .bed     (--check prints TP/FN/FP counts against these)

    python scripts/fetch_dv_testdata.py [--dataset hg003-chr20]
    python scripts/run_local.py --sample <sample> --reference <reference> \\
        --caller deepvariant --skip-to-variants --check

--pangenome also gets the HPRC v1.1 Minigraph-Cactus pangenome graph on GRCh38 (the graph Google's
pangenome-aware case studies use), from the Human Pangenome Reference Consortium's public bucket, for
pangenome-aware DeepVariant (run_local.py --caller pangenome). Only valid with GRCh38 datasets.

  --pangenome / --pangenome chr20  HPRC's chr20 graph (chroms/chr20.vg, 1.28 GB download), converted with vg
                                   (Docker, quay.io/vgteam/vg) to <root>/pangenome/hprc-v1.1-mc-grch38.chr20.gbz
                                   (72 MB; same 46 samples, 90 haplotypes and GRCh38/CHM13 references).
  --pangenome full                 the whole-genome graph as published (3.29 GB) to
                                   <root>/pangenome/hprc-v1.1-mc-grch38.gbz. Loaded, it needs ~10.7 GB of shared
                                   memory plus ~2 GB per make_examples worker.

--stratifications (GRCh38 datasets) also gets a few GIAB v3.3 genome stratifications (difficult regions, segmental
duplications, low mappability), cut down to the dataset's contig, to stratifications/GRCh38_<contig>/ with a hap.py
TSV at stratifications/GRCh38_<contig>.tsv. benchmark_happy.py --stratification reports accuracy per region type:
a pangenome should help most where a linear reference does worst.
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
HPRC_GRCH38 = (
    "https://s3-us-west-2.amazonaws.com/human-pangenomics/pangenomes/freeze/freeze1/minigraph-cactus/"
    "hprc-v1.1-mc-grch38/"
)
HPRC_GRCH38_R2_EVAL = (
    "https://s3-us-west-2.amazonaws.com/human-pangenomics/pangenomes/freeze/release2/minigraph-cactus/v2.0/"
    "benchmark-graphs/hprc-v2.0-mc-grch38-eval/"
)
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
    # HPRC release 2 (v2.0), the benchmark ("-eval") build: HG002, HG005 and NA19240 are held out (HPRC README), so
    # it is the one to score GIAB samples against. HG002 is HG003's son: the main r2 graph holds one of HG003's own
    # haplotypes. Its published chroms/chr20.vg is 11 GB (464 haplotypes) and needs ~22 GB to convert, so chr20 is cut
    # out of the whole-genome GBZ instead (vg chunk -C on the GRCh38 chr20 path, then vg gbwt), see gbz_chunk_command.
    "r2-chr20": {
        "url": HPRC_GRCH38_R2_EVAL + "hprc-v2.0-mc-grch38-eval.gbz",
        "size": 5386150200,
        "key": "pangenome/hprc-v2.0-mc-grch38-eval.gbz",
        "build": "pangenome/hprc-v2.0-mc-grch38-eval.chr20.gbz",
        "from": "gbz",
        "path": "GRCh38#0#chr20",
    },
    # A FIRST LOOK, NOT THE SAME KIND OF GRAPH: chr20 built from the r2 benchmark build's VCF (vg construct + vg gbwt)
    # instead of cut from its Minigraph-Cactus GBZ, because cutting needs ~18-20 GB of memory (see "r2-chr20").
    # Top-level sites only (INFO/LV=0); overlapping variants are dropped per haplotype (--discard-overlaps); same
    # 229 samples (HG002, HG005, NA19240 held out). An r1.1-vs-this difference can partly be the construction method.
    "r2-chr20-vcf": {
        "url": HPRC_GRCH38_R2_EVAL + "hprc-v2.0-mc-grch38-eval.vcf.gz",
        "build": "pangenome/hprc-v2.0-mc-grch38-eval.chr20.vcf-construct.gbz",
        "from": "vcf",
        "contig": "chr20",
        "path": "GRCh38#0#chr20",
    },
}
GRCH38_DATASETS = {"hg003-chr20", "hg003-chr20-vg", "hg003-30x-chr20", "hg005-chr20"}
# vg release used by Google's pangenome-aware VG case study.
VG_IMAGE = "quay.io/vgteam/vg:v1.55.0"
# The whole-genome graph marks both references; keep them as references in the chr20 graph too.
REFERENCE_SAMPLES = ("GRCh38", "CHM13")

# Whole-genome BWA BAMs of the GIAB samples in Google's public bucket (hs38DH, NovaSeq PCR-free), and GIAB's FTP.
BRAIN_30X = (
    "https://storage.googleapis.com/brain-genomics-public/research/sequencing/grch38/bam/novaseq/wgs_pcr_free/30x/"
)
GIAB = "https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/"
HG005_TRUTH = GIAB + "ChineseTrio/HG005_NA24631_son/NISTv4.2.1/GRCh38/HG005_GRCh38_1_22_v4.2.1_benchmark"
# samtools with HTTP support, for cutting one contig out of a remote BAM: the pipeline image has it.
APP_IMAGE = "geyser-app"

# GIAB v3.3 genome stratifications for --stratifications: (hap.py region name, path under STRAT_BASE).
STRAT_BASE = GIAB + "genome-stratifications/v3.3/GRCh38@all/"
STRATIFICATIONS = (
    ("alldifficultregions", "Union/GRCh38_alldifficultregions.bed.gz"),
    ("notinalldifficultregions", "Union/GRCh38_notinalldifficultregions.bed.gz"),
    ("alllowmapandsegdupregions", "Union/GRCh38_alllowmapandsegdupregions.bed.gz"),
    ("segdups", "SegmentalDuplications/GRCh38_segdups.bed.gz"),
    ("lowmappabilityall", "Mappability/GRCh38_lowmappabilityall.bed.gz"),
)

# Each file: (remote path, expected bytes or None, local key, how to store it)
#   copy          store as downloaded
#   gunzip        decompress
#   fasta:<ctg>   fetch only that contig from an uncompressed FASTA, by HTTP range (size from its .fai)
#   only:<ctg>    keep only that contig's rows (VCF keeps its header; gzipped if the local key ends .gz)
#   region:<ctg>  fetch only that contig's reads from a remote indexed BAM (samtools in Docker)
# Gene models for consequence annotation (bcftools csq, the variants stage): Ensembl's per-chromosome
# GFF3, which names contigs without "chr". "ensembl:<contig>" renames them to match the GRCh38 FASTA.
ENSEMBL_RELEASE = 115
ENSEMBL_GFF = (
    f"https://ftp.ensembl.org/pub/release-{ENSEMBL_RELEASE}/gff3/homo_sapiens/"
    f"Homo_sapiens.GRCh38.{ENSEMBL_RELEASE}.chromosome.{{n}}.gff3.gz"
)
ENSEMBL_GFF_SIZES = {"20": 2019220}

DATASETS = {
    "na12878-slice": {
        "sample": "NA12878_S1",
        "reference": "ucsc.hg19.chr20.unittest.fasta",
        "files": [
            ("quickstart-testdata/NA12878_S1.chr20.10_10p1mb.bam", 3925783, "alignments/NA12878_S1.bam", "copy"),
            (
                "quickstart-testdata/ucsc.hg19.chr20.unittest.fasta.gz",
                606944,
                "reference/ucsc.hg19.chr20.unittest.fasta",
                "gunzip",
            ),
            ("quickstart-testdata/test_nist.b37_chr20_100kbp_at_10mb.vcf.gz", 5898, "truth/NA12878_S1.vcf.gz", "copy"),
            ("quickstart-testdata/test_nist.b37_chr20_100kbp_at_10mb.bed", 264, "truth/NA12878_S1.bed", "copy"),
        ],
    },
    "hg003-chr20": {
        "sample": "HG003_chr20",
        "reference": "GRCh38_chr20.fa",
        "files": [
            (
                "case-study-testdata/HG003.novaseq.pcr-free.35x.dedup.grch38_no_alt.chr20.bam",
                1112015824,
                "alignments/HG003_chr20.bam",
                "copy",
            ),
            (GRCH38, None, "reference/GRCh38_chr20.fa", "fasta:chr20"),
            (
                "case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark.vcf.gz",
                147212428,
                "truth/HG003_chr20.vcf.gz",
                "only:chr20",
            ),
            (
                "case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed",
                13219856,
                "truth/HG003_chr20.bed",
                "only:chr20",
            ),
        ],
    },
    "hg003-chr20-vg": {
        "sample": "HG003_chr20_vg",
        "reference": "GRCh38_chr20.fa",
        "files": [
            (
                "vg-case-study/HG003.novaseq.pcr-free.35x.vg-1.55.0.chr20.bam",
                947058313,
                "alignments/HG003_chr20_vg.bam",
                "copy",
            ),
            (GRCH38, None, "reference/GRCh38_chr20.fa", "fasta:chr20"),
            (
                "case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark.vcf.gz",
                147212428,
                "truth/HG003_chr20_vg.vcf.gz",
                "only:chr20",
            ),
            (
                "case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed",
                13219856,
                "truth/HG003_chr20_vg.bed",
                "only:chr20",
            ),
        ],
    },
    "hg005-chr20": {
        "sample": "HG005_chr20",
        "reference": "GRCh38_chr20.fa",
        "files": [
            (
                BRAIN_30X + "HG005.novaseq.pcr-free.30x.dedup.grch38.bam",
                None,
                "alignments/HG005_chr20.bam",
                "region:chr20",
            ),
            (GRCH38, None, "reference/GRCh38_chr20.fa", "fasta:chr20"),
            (HG005_TRUTH + ".vcf.gz", 139334496, "truth/HG005_chr20.vcf.gz", "only:chr20"),
            (HG005_TRUTH + ".bed", 18615340, "truth/HG005_chr20.bed", "only:chr20"),
        ],
    },
    "hg003-30x-chr20": {
        "sample": "HG003_30x_chr20",
        "reference": "GRCh38_chr20.fa",
        "files": [
            (
                BRAIN_30X + "HG003.novaseq.pcr-free.30x.dedup.grch38.bam",
                None,
                "alignments/HG003_30x_chr20.bam",
                "region:chr20",
            ),
            (GRCH38, None, "reference/GRCh38_chr20.fa", "fasta:chr20"),
            (
                "case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark.vcf.gz",
                147212428,
                "truth/HG003_30x_chr20.vcf.gz",
                "only:chr20",
            ),
            (
                "case-study-testdata/HG003_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed",
                13219856,
                "truth/HG003_30x_chr20.bed",
                "only:chr20",
            ),
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
                print(
                    f"    {done / 1e6:,.0f} MB" + (f" / {expected_size / 1e6:,.0f} MB" if expected_size else ""),
                    flush=True,
                )
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
    """Copy src to dest keeping header lines and rows on one contig. Returns rows kept.

    Each side is gzip if its name ends .gz, so a .bed.gz can be cut down to a plain .bed.
    """
    open_in = gzip.open if src.endswith(".gz") else open
    open_out = gzip.open if dest.endswith(".gz") else open
    prefix, kept = contig + "\t", 0
    # newline="" keeps line endings byte-for-byte (no \r\n on Windows)
    with (
        open_in(src, "rt", encoding="utf-8", newline="") as fin,
        open_out(dest + ".part", "wt", encoding="utf-8", newline="") as fout,
    ):
        for line in fin:
            if line.startswith("#") or line.startswith(prefix):
                fout.write(line)
                kept += not line.startswith("#")
    os.replace(dest + ".part", dest)
    return kept


def ensembl_gff_for_contig(src, dest, contig):
    """Ensembl GFF3 (contig '20') -> gzip GFF3 named like the reference ('chr20'). Returns rows kept.

    Keeps the headers (renaming ##sequence-region) and the rows on that contig; drops other contigs.
    """
    ensembl = contig.removeprefix("chr")
    prefix, kept = ensembl + "\t", 0
    with (
        gzip.open(src, "rt", encoding="utf-8", newline="") as fin,
        gzip.open(dest + ".part", "wt", encoding="utf-8", newline="") as fout,
    ):
        for line in fin:
            if line.startswith("##sequence-region"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == ensembl:
                    fout.write(line.replace(f" {ensembl} ", f" {contig} ", 1))
            elif line.startswith("#"):
                fout.write(line)
            elif line.startswith(prefix):
                fout.write(contig + line[len(ensembl) :])
                kept += 1
    os.replace(dest + ".part", dest)
    return kept


def annotation_files(name):
    """The file entry for a GRCh38 dataset's gene models: reference/<reference>.gff3.gz."""
    contig = dataset_contig(name)
    n = contig.removeprefix("chr")
    return [
        (
            ENSEMBL_GFF.format(n=n),
            ENSEMBL_GFF_SIZES.get(n),
            f"reference/{DATASETS[name]['reference']}.gff3.gz",
            f"ensembl:{contig}",
        )
    ]


def remote_url(remote):
    """Full URL for a file entry: absolute URLs as-is, anything else under DeepVariant's bucket."""
    return remote if remote.startswith(("https://", "http://")) else BASE_URL + remote


def dataset_contig(name):
    """The one contig a GRCh38 dataset covers (from its reference FASTA entry)."""
    for _, _, _, how in DATASETS[name]["files"]:
        if how.startswith("fasta:"):
            return how.split(":", 1)[1]
    raise ValueError(f"{name} has no single-contig reference")


def stratification_files(contig):
    """File entries for the GIAB stratification BEDs, cut down to one contig as plain BEDs."""
    return [
        (STRAT_BASE + path, None, f"stratifications/GRCh38_{contig}/{name}.bed", f"only:{contig}")
        for name, path in STRATIFICATIONS
    ]


def stratification_tsv(contig):
    """(key, text) of the hap.py --stratification TSV: region name, then BED path relative to the TSV."""
    text = "".join(f"{name}\tGRCh38_{contig}/{name}.bed\n" for name, _ in STRATIFICATIONS)
    return f"stratifications/GRCh38_{contig}.tsv", text


def dataset_files(name, pangenome=None, stratifications=False, annotation=False):
    """The file entries to fetch for a dataset, plus a pangenome graph ('chr20' or 'full'), the GIAB
    stratification BEDs and Ensembl gene models (for bcftools csq) when asked.

    Pangenome graphs and the stratifications are on GRCh38, so only GRCh38 datasets accept them.
    """
    files = list(DATASETS[name]["files"])
    if (pangenome or stratifications or annotation) and name not in GRCH38_DATASETS:
        flag = "--pangenome" if pangenome else "--stratifications" if stratifications else "--annotation"
        raise ValueError(
            f"{flag} needs a GRCh38 dataset ({', '.join(sorted(GRCH38_DATASETS))}); {name} is on another reference"
        )
    if pangenome:
        graph = PANGENOMES[pangenome]
        if "key" in graph:  # a VCF-built graph reads its VCF remotely while building: nothing to download first
            files.append((graph["url"], graph["size"], graph["key"], "copy"))
    if stratifications:
        files += stratification_files(dataset_contig(name))
    if annotation:
        files += annotation_files(name)
    return files


def bam_region_command(root, url, key, contig, image=APP_IMAGE):
    """docker run that copies one contig's reads out of a remote indexed BAM to <root>/<key>.

    samtools reads the remote .bai, then fetches only that contig's byte ranges over HTTP. Reads whose mate maps to
    another contig are kept, as in Google's own per-chromosome BAMs. The result is checked with samtools quickcheck;
    a failed fetch (e.g. a dropped connection) removes its partial file.
    """
    out = f"/data/{key}"
    script = (
        f"set -e; cd /tmp; trap 'rm -f {out}.part' ERR; samtools view -b -o {out}.part {url} {contig}; "
        f"samtools quickcheck {out}.part; mv {out}.part {out}"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--mount",
        f"type=bind,source={os.path.abspath(root)},target=/data",
        image,
        "bash",
        "-c",
        script,
    ]


def vg_build_command(root, vg_key, gbz_key, memory="8g"):
    """docker run that converts a .vg graph to GBZ (via GFA) with the HPRC reference samples kept as references.

    vg convert -f writes the haplotypes as GFA walks; vg gbwt -G indexes them and stores graph + index as GBZ.
    The GFA is a temporary file (about 1 GB for chr20) and is removed afterwards.
    """
    vg, gbz = f"/data/{vg_key}", f"/data/{gbz_key}"
    gfa = gbz + ".gfa"
    refs = " ".join(f"--set-reference {s}" for s in REFERENCE_SAMPLES)
    script = (
        f"set -e; vg convert -f {vg} > {gfa}; "
        f"vg gbwt -G {gfa} --gbz-format -g {gbz}.part {refs}; mv {gbz}.part {gbz}; rm -f {gfa}"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--memory",
        memory,
        "--mount",
        f"type=bind,source={os.path.abspath(root)},target=/data",
        VG_IMAGE,
        "bash",
        "-c",
        script,
    ]


def gbz_chunk_command(root, gbz_key, out_key, path, memory="20g"):
    """docker run that cuts one chromosome out of a whole-genome GBZ, haplotypes included, into its own GBZ.

    vg chunk -C writes the connected component holding the reference path (Minigraph-Cactus graphs have one
    component per chromosome) as GFA with every haplotype in it; vg gbwt -G indexes those walks into a GBZ, keeping
    the HPRC reference samples as references, as vg_build_command does for a .vg. The GFA is removed afterwards.
    """
    gbz, out = f"/data/{gbz_key}", f"/data/{out_key}"
    gfa = out + ".gfa"
    refs = " ".join(f"--set-reference {s}" for s in REFERENCE_SAMPLES)
    script = (
        f"set -e; vg chunk -x {gbz} -p {path} -C -O gfa > {gfa}; "
        f"vg gbwt -G {gfa} --gbz-format -g {out}.part {refs}; mv {out}.part {out}; rm -f {gfa}"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--memory",
        memory,
        "--mount",
        f"type=bind,source={os.path.abspath(root)},target=/data",
        VG_IMAGE,
        "bash",
        "-c",
        script,
    ]


def vcf_graph_commands(root, graph, reference_key, app_image=APP_IMAGE, memory="12g"):
    """Two docker runs that build a chromosome graph from a pangenome VCF ("from": "vcf" entries).

    1. geyser-app (bcftools, samtools): the contig's top-level sites (INFO/LV=0) read remotely by index, with the
       contig renamed to the reference path name (GRCh38#0#chr20, what pangenome-aware DeepVariant looks for), and
       the reference FASTA renamed the same way.
    2. vg: vg construct (with alt paths), the VCF haplotypes (vg gbwt -v, --discard-overlaps) merged with the
       reference path (vg gbwt -E), then the GBZ with GRCh38 as the reference sample.
    Work files stay in pangenome/<build stem>.work/ next to the GBZ.
    """
    contig, path = graph["contig"], graph["path"]
    gbz = f"/data/{graph['build']}"
    work = gbz[: -len(".gbz")] + ".work"
    prep = (
        f"set -e -o pipefail; mkdir -p {work}; cd {work}; "
        f"samtools faidx /data/{reference_key} {contig} | sed '1s/.*/>{path}/' > ref.fa; samtools faidx ref.fa; "
        f"bcftools view -i 'INFO/LV=0' -r {contig} {graph['url']} "
        f'| awk \'BEGIN{{OFS="\\t"}} /^##contig/{{next}} '
        f'/^#CHROM/{{print "##contig=<ID={path}>"; print; next}} /^#/{{print; next}} {{$1="{path}"; print}}\' '
        f"| bcftools view -Oz -o vars.vcf.gz -; bcftools index -t vars.vcf.gz"
    )
    build = (
        f"set -e; cd {work}; trap 'rm -f {gbz}.part' ERR; "
        f"vg construct -r ref.fa -v vars.vcf.gz -a -m 1024 -t 8 > graph.vg; "
        f"vg gbwt -x graph.vg -o haplotypes.gbwt -v vars.vcf.gz --discard-overlaps; "
        f"vg gbwt -x graph.vg -o reference.gbwt -E; "
        f"vg gbwt -m -o all.gbwt haplotypes.gbwt reference.gbwt; "
        f"vg gbwt -x graph.vg all.gbwt --gbz-format -g {gbz}.part --set-reference GRCh38; mv {gbz}.part {gbz}"
    )
    mount = ["--mount", f"type=bind,source={os.path.abspath(root)},target=/data"]
    return [
        ["docker", "run", "--rm", "--memory", "4g", "-w", "/tmp", *mount, app_image, "bash", "-c", prep],
        ["docker", "run", "--rm", "--memory", memory, *mount, VG_IMAGE, "bash", "-c", build],
    ]


def build_chr20_gbz(root, force=False, pangenome="chr20", reference_key="reference/GRCh38_chr20.fa"):
    """Build the chromosome GBZ a graph entry names in "build": from a .vg (r1.1) or cut from a whole GBZ (r2)."""
    graph = PANGENOMES[pangenome]
    dest = os.path.join(root, *graph["build"].split("/"))
    if os.path.exists(dest) and not force:
        print(f"  exists   {dest}")
        return
    if graph.get("from") == "vcf":
        print(f"  building {graph['build']} from the {graph['contig']} sites of {graph['url']} (VCF construction)")
        for cmd in vcf_graph_commands(root, graph, reference_key):
            subprocess.run(cmd, check=True)
        print(f"  wrote    {dest} ({os.path.getsize(dest):,} bytes)", flush=True)
        return
    if graph.get("from") == "gbz":
        print(f"  building {graph['build']} from {graph['key']} with {VG_IMAGE} (vg chunk + vg gbwt)", flush=True)
        cmd = gbz_chunk_command(root, graph["key"], graph["build"], graph["path"])
    else:
        print(f"  building {graph['build']} with {VG_IMAGE} (about 4 minutes, under 3 GB of memory)", flush=True)
        cmd = vg_build_command(root, graph["key"], graph["build"])
    subprocess.run(cmd, check=True)
    print(f"  wrote    {dest} ({os.path.getsize(dest):,} bytes)", flush=True)


def fetch_dataset(
    name, root, force=False, pangenome=None, stratifications=False, app_image=APP_IMAGE, annotation=False
):
    ds = DATASETS[name]
    files = dataset_files(name, pangenome, stratifications, annotation)
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
        elif how.startswith("region:"):
            contig = how.split(":", 1)[1]
            print(f"    {contig} only, with samtools in {app_image} (about 1 GB for 30x chr20)", flush=True)
            if subprocess.run(["docker", "image", "inspect", app_image], capture_output=True).returncode != 0:
                raise RuntimeError(
                    f"Docker image {app_image} not found: build it first "
                    f"(docker build -f Dockerfile -t {app_image} .) or pass --app-image"
                )
            subprocess.run(bam_region_command(root, url, key, contig, app_image), check=True)
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
            elif how.startswith("ensembl:"):
                tmp = raw + ".gz"
                os.replace(raw, tmp)
                rows = ensembl_gff_for_contig(tmp, dest, how.split(":", 1)[1])
                os.remove(tmp)
                print(f"    kept {rows:,} features, contig renamed to {how.split(':', 1)[1]}")
            else:
                raise ValueError(how)
        print(f"  wrote    {dest} ({os.path.getsize(dest):,} bytes)", flush=True)
    if pangenome and "build" in PANGENOMES[pangenome]:
        build_chr20_gbz(root, force, pangenome)
    if stratifications:
        tsv_key, text = stratification_tsv(dataset_contig(name))
        with open(os.path.join(root, *tsv_key.split("/")), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"  wrote    {tsv_key} (for benchmark_happy.py --stratification {tsv_key})")
    print(f"Done. Sample {ds['sample']}, reference {ds['reference']}.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="local_bucket", help="storage root (LOCAL_STORAGE_DIR)")
    p.add_argument("--dataset", choices=sorted(DATASETS), default="na12878-slice")
    p.add_argument("--force", action="store_true", help="download again even if the files exist")
    p.add_argument(
        "--pangenome",
        nargs="?",
        const="chr20",
        choices=sorted(PANGENOMES),
        help="also get an HPRC GRCh38 pangenome graph for --caller pangenome: chr20 (default; v1.1, built "
        "locally with vg, 72 MB), full (v1.1, 3.29 GB, needs ~11 GB of shared memory) or r2-chr20 (release 2 "
        "benchmark graph without HG002/HG005, 5.4 GB download, chr20 cut out locally)",
    )
    p.add_argument(
        "--stratifications",
        action="store_true",
        help="also get GIAB v3.3 stratification BEDs (difficult regions, segdups, low mappability) "
        "for benchmark_happy.py --stratification",
    )
    p.add_argument(
        "--annotation",
        action="store_true",
        help=f"also get Ensembl {ENSEMBL_RELEASE} gene models for the dataset's chromosome, as "
        "reference/<reference>.gff3.gz, so the variants stage annotates consequences (bcftools csq)",
    )
    p.add_argument("--app-image", default=APP_IMAGE, help="image with samtools, for datasets cut from remote BAMs")
    a = p.parse_args(argv)
    try:
        fetch_dataset(a.dataset, a.root, a.force, a.pangenome, a.stratifications, a.app_image, a.annotation)
    except ValueError as err:
        p.error(str(err))


if __name__ == "__main__":
    main()
