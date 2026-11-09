# ./Dockerfile
#
# The pipeline image: every stage (qc, align, variants) runs in it, locally and on AWS Batch (Fargate).
# Kept to what the stages run, because each Fargate task pulls the whole image before it starts (#141):
# fastp, bwa, samtools, bcftools (Debian), bwa-mem2 and mosdepth (upstream, checksum-pinned), Python
# with boto3, and the quality gate (gate/, built from source in its own stage). Everything only needed
# to fetch or build those (curl, bzip2, the Rust toolchain) stays in the earlier stages.
#
# Debian 13 (trixie). Debian 11 (bullseye) security updates were removed from the mirrors, so fresh
# builds on it fail with 404s. Base pinned by digest so builds are reproducible; Dependabot proposes the
# new digest when the tag is rebuilt (Debian and Python security patches). Keep both FROM lines equal.

# ---------------------------------------------------------------------------------------------- download
FROM python:3.11-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60 AS download

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl bzip2 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# bwa-mem2, the default aligner (ALIGNER=bwa switches back to the apt bwa below). Debian has no
# package, so this is the upstream release, checksum-pinned. It ships one binary per instruction
# set (SSE4.1 ... AVX-512) and a `bwa-mem2` launcher that runs the best one for the CPU it finds,
# so the same image is fast on whatever hardware Fargate hands out. The launcher looks for those
# binaries next to itself, hence PATH rather than a symlink. The v2.3 sources still say
# PACKAGE_VERSION "2.2.1", so `bwa-mem2 version` and the BAM @PG line report 2.2.1: expected.
ARG BWA_MEM2_VERSION=2.3
ARG BWA_MEM2_SHA256=112f3a3ebf3f8c2377f52d61446ead392c4a98ecf89e7629617d3ed16f4e73cb
RUN curl -fsSL "https://github.com/bwa-mem2/bwa-mem2/releases/download/v${BWA_MEM2_VERSION}/bwa-mem2-${BWA_MEM2_VERSION}_x64-linux.tar.bz2" \
        -o /tmp/bwa-mem2.tar.bz2 && \
    echo "${BWA_MEM2_SHA256}  /tmp/bwa-mem2.tar.bz2" | sha256sum -c - && \
    mkdir -p /opt/bwa-mem2 && \
    tar -xjf /tmp/bwa-mem2.tar.bz2 -C /opt/bwa-mem2 --strip-components=1 --no-same-owner && \
    rm /tmp/bwa-mem2.tar.bz2

# mosdepth: coverage statistics after align (stats/{id}/mosdepth.*.txt). Not packaged in Debian 13;
# the upstream release is a single static binary, checksum-pinned like bwa-mem2.
ARG MOSDEPTH_VERSION=0.3.15
ARG MOSDEPTH_SHA256=8abd890f5ceeddebc10315cfdef5b909713be4fb67610b7d089f4dc25cf0cde2
RUN curl -fsSL "https://github.com/brentp/mosdepth/releases/download/v${MOSDEPTH_VERSION}/mosdepth" \
        -o /opt/mosdepth && \
    echo "${MOSDEPTH_SHA256}  /opt/mosdepth" | sha256sum -c - && \
    chmod +x /opt/mosdepth

# ---------------------------------------------------------------------------------------------- gate
# The quality gate (gate/, #144): one static binary (musl), so the final image gets the binary and no
# Rust toolchain. The Rust version must match gate/rust-toolchain.toml (bump both together; Dependabot
# ignores this tag for that reason). RUSTUP_TOOLCHAIN uses the image's toolchain as it is, instead of
# letting the toolchain file download clippy and rustfmt, which a build doesn't need.
FROM rust:1.99.1-slim-trixie@sha256:b8e062b1c3a80183df83d9ba187cadd2b5ab84bc78d453dbfeb2fccd5f982247 AS gate
ENV RUSTUP_TOOLCHAIN=1.99.1
RUN rustup target add x86_64-unknown-linux-musl
WORKDIR /gate
COPY gate/ .
RUN cargo build --release --locked --target x86_64-unknown-linux-musl && \
    cp target/x86_64-unknown-linux-musl/release/geyser-gate /usr/local/bin/geyser-gate && \
    geyser-gate --version

# ---------------------------------------------------------------------------------------------- final
FROM python:3.11-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

# Only the tools the stages run. fastp replaced FastQC (no Java). --no-install-recommends keeps out
# perl, which bwa and bcftools only suggest for helper scripts the pipeline doesn't use. Not here any
# more (#141): the AWS CLI (S3 goes through boto3), sra-toolkit (reads come from S3 or local_bucket/),
# curl, unzip, bzip2, openssl.
RUN apt-get update && \
    apt-get install -y --no-install-recommends fastp bwa samtools bcftools pigz && \
    rm -rf /var/lib/apt/lists/* && \
    fastp --version && samtools --version | head -1 && bcftools --version | head -1

ARG BWA_MEM2_VERSION=2.3
ARG MOSDEPTH_VERSION=0.3.15
COPY --from=download /opt/bwa-mem2 /opt/bwa-mem2
COPY --from=download /opt/mosdepth /usr/local/bin/mosdepth
# The gate and its shipped thresholds (GATE_CONFIG=$GATE_THRESHOLDS_DIR/synthetic.toml for the test data).
COPY --from=gate /usr/local/bin/geyser-gate /usr/local/bin/geyser-gate
COPY --from=gate /gate/thresholds /opt/geyser-gate/thresholds
# The run record (app/provenance.py) shows the pinned releases next to the versions the binaries report.
ENV PATH="/opt/bwa-mem2:${PATH}" \
    GEYSER_BWA_MEM2_RELEASE=${BWA_MEM2_VERSION} \
    GEYSER_MOSDEPTH_RELEASE=${MOSDEPTH_VERSION} \
    GATE_THRESHOLDS_DIR=/opt/geyser-gate/thresholds
RUN bwa-mem2 version && mosdepth --version && geyser-gate --version

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
