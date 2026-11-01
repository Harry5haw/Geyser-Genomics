# ./Dockerfile

# Debian 13 (trixie). Debian 11 (bullseye) security updates were removed from the mirrors,
# so fresh builds on it fail with 404s. FastQC needs fontconfig, a font and libharfbuzz (for
# Java's font manager) to draw its report charts; without them it throws inside a worker
# thread and hangs instead of exiting.
FROM python:3.11-slim-trixie

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless fontconfig fonts-dejavu-core libharfbuzz0b wget unzip bwa samtools bcftools perl sra-toolkit curl openssl \
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

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application and debug scripts
COPY app/ .
