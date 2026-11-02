# ./Dockerfile

# Debian 13 (trixie). Debian 11 (bullseye) security updates were removed from the mirrors,
# so fresh builds on it fail with 404s. QC is fastp from Debian (0.24.0 in trixie): a C++
# binary, so the image needs no Java, fonts or FastQC download any more.
# Pinned by digest so builds are reproducible; Dependabot proposes the new digest when the tag is rebuilt
# (Debian and Python security patches). The tag stays for readers and for Dependabot.
FROM python:3.11-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    fastp unzip bzip2 bwa samtools bcftools perl sra-toolkit curl openssl \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    fastp --version

RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip ./aws

RUN mkdir -p /root/.ncbi && chmod -R 777 /root/.ncbi

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

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application and debug scripts
COPY app/ .
