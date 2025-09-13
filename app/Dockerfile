# app/Dockerfile

# Stage 1: Base Image
FROM python:3.11-slim-bullseye

# -----------------------------------------------------------------------------
# System-level dependencies installation
# -----------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Existing bioinformatics tools (unchanged)
    openjdk-11-jre-headless \
    wget \
    unzip \
    bwa \
    samtools \
    bcftools \
    perl  \
    sra-toolkit \
    # --- ADDED FOR DIAGNOSTICS ---
    curl \
    openssl \
    # ---------------------------
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# --- ADDED FOR DIAGNOSTICS: Install the AWS CLI v2 ---
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip ./aws

# Pre-create the NCBI configuration directory (unchanged)
RUN mkdir -p /root/.ncbi && chmod -R 777 /root/.ncbi

# Manually install FastQC (unchanged)
RUN wget -q https://www.bioinformatics.babraham.ac.uk/projects/fastqc/fastqc_v0.12.1.zip -O /tmp/fastqc.zip && \
    unzip /tmp/fastqc.zip -d /opt/ && \
    rm /tmp/fastqc.zip && \
    chmod +x /opt/FastQC/fastqc && \
    ln -s /opt/FastQC/fastqc /usr/local/bin/fastqc

# -----------------------------------------------------------------------------
# Python application setup
# -----------------------------------------------------------------------------
WORKDIR /app

# Copy the Python requirements file and install dependencies (unchanged)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- MODIFIED: Copy both the application and the new debug script ---
COPY tasks.py .
COPY debug_network.py .

# The command to run is provided by the AWS Batch job definition override,
# so no ENTRYPOINT or CMD is needed here.
