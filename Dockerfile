# ./Dockerfile (The one true Dockerfile for the project)

FROM python:3.11-slim-bullseye

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    openjdk-11-jre-headless wget unzip bwa samtools bcftools perl sra-toolkit curl openssl \
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

# CORRECTED: Copy requirements from the app/ directory
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# CORRECTED: Copy all application source code from the app/ directory into the container
COPY app/ .
