# ./Dockerfile

# Debian 13 (trixie). Debian 11 (bullseye) security updates were removed from the mirrors,
# so fresh builds on it fail with 404s. FastQC needs fontconfig, a font and libharfbuzz (for
# Java's font manager) to draw its report charts; without them it throws inside a worker
# thread and hangs instead of exiting.
# Pinned by digest so builds are reproducible; Dependabot proposes the new digest when the tag is rebuilt
# (Debian and Python security patches). The tag stays for readers and for Dependabot.
FROM python:3.11-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless fontconfig fonts-dejavu-core libharfbuzz0b wget unzip bzip2 bwa samtools bcftools perl sra-toolkit curl openssl \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip ./aws

RUN mkdir -p /root/.ncbi && chmod -R 777 /root/.ncbi

RUN wget -q https://www.bioinformatics.babraham.ac.uk/projects/fastqc/fastqc_v0.12.1.zip -O /tmp/fastqc.zip && \
    unzip /tmp/fastqc.zip -d /opt/ && \
    rm /tmp/fastqc.zip && \
    chmod +x /opt/FastQC/fastqc && \
    ln -s /opt/FastQC/fastqc /usr/local/bin/fastqc

# bwa-mem2, the default aligner (ALIGNER=bwa switches back to the apt bwa above). Debian has no
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
    rm /tmp/bwa-mem2.tar.bz2 && \
    /opt/bwa-mem2/bwa-mem2 version
ENV PATH="/opt/bwa-mem2:${PATH}"

# mosdepth: coverage statistics after align (stats/{id}/mosdepth.*.txt). Not packaged in Debian 13;
# the upstream release is a single static binary, checksum-pinned like bwa-mem2.
ARG MOSDEPTH_VERSION=0.3.15
ARG MOSDEPTH_SHA256=8abd890f5ceeddebc10315cfdef5b909713be4fb67610b7d089f4dc25cf0cde2
RUN curl -fsSL "https://github.com/brentp/mosdepth/releases/download/v${MOSDEPTH_VERSION}/mosdepth" \
        -o /usr/local/bin/mosdepth && \
    echo "${MOSDEPTH_SHA256}  /usr/local/bin/mosdepth" | sha256sum -c - && \
    chmod +x /usr/local/bin/mosdepth && \
    mosdepth --version

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application and debug scripts
COPY app/ .
