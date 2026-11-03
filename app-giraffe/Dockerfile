# app-giraffe/Dockerfile
#
# The align stage with ALIGNER=giraffe (#146): geyser-app plus vg (graph alignment) and kmc (the k-mer
# counts haplotype sampling needs). Everything else (tasks.py, samtools for fixmate | sort | markdup and
# CRAM, mosdepth, Python) is geyser-app's, so the two images can't drift apart. Build geyser-app first,
# from the repo root:
#
#   docker build -f Dockerfile -t geyser-app .
#   docker build -f app-giraffe/Dockerfile -t geyser-giraffe .          # BASE_IMAGE=geyser-app by default
#
# Run (normally via scripts/run_local.py --aligner giraffe):
#   docker run --rm ... geyser-giraffe python tasks.py align <id> <reference_name>
#
# Separate from geyser-app because every bwa-mem2, QC, variants and gate job pulls geyser-app before it
# starts (#141/#142): vg and kmc would add ~19 MB compressed (~22% of its 87 MB) to each of those pulls for a tool
# only the giraffe align job runs. A whole-genome giraffe job runs for hours, so its extra layer is noise.

ARG BASE_IMAGE=geyser-app

# ---------------------------------------------------------------------------------------------- download
FROM python:3.11-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60 AS download

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# vg: the release Google's VG case study and our benchmarks (#129, scripts/benchmark_giraffe.py) use. Upstream
# ships one static binary; checksum-pinned like bwa-mem2. Its bundled samtools is 1.10, too old for our
# CRAM/markdup flags, which is why post-processing uses geyser-app's samtools.
ARG VG_VERSION=1.55.0
ARG VG_SHA256=2c602084b4eab7b50147fba31f51e126135d7ceaaf405632445bff96d68ba6c7
RUN curl -fsSL "https://github.com/vgteam/vg/releases/download/v${VG_VERSION}/vg" -o /opt/vg && \
    echo "${VG_SHA256}  /opt/vg" | sha256sum -c - && \
    chmod +x /opt/vg

# kmc: k-mer counts (KFF) for vg giraffe's haplotype sampling. The upstream x64 release is static.
ARG KMC_VERSION=3.2.4
ARG KMC_SHA256=158f2084f8d928b3f33b8aaf7d1220fee4183bf46837787e5e6b16bbdf54d31d
RUN curl -fsSL "https://github.com/refresh-bio/KMC/releases/download/v${KMC_VERSION}/KMC${KMC_VERSION}.linux.x64.tar.gz" \
        -o /tmp/kmc.tar.gz && \
    echo "${KMC_SHA256}  /tmp/kmc.tar.gz" | sha256sum -c - && \
    tar -xzf /tmp/kmc.tar.gz -C /opt --no-same-owner bin/kmc && \
    rm /tmp/kmc.tar.gz

# ---------------------------------------------------------------------------------------------- final
FROM ${BASE_IMAGE}

ARG VG_VERSION=1.55.0
ARG KMC_VERSION=3.2.4
COPY --from=download /opt/vg /usr/local/bin/vg
COPY --from=download /opt/bin/kmc /usr/local/bin/kmc
# ALIGNER=giraffe: this image exists to run that align stage (a job can still override it). The releases
# go into the run record next to the versions the binaries report (app/provenance.py).
ENV ALIGNER=giraffe \
    GEYSER_VG_RELEASE=${VG_VERSION} \
    GEYSER_KMC_RELEASE=${KMC_VERSION}
# grep, not head: the check fails if either binary can't run here (a pipe's status is its last command's).
RUN vg version | grep -F "vg version v${VG_VERSION}" && kmc 2>&1 | grep -F "ver. ${KMC_VERSION}"
