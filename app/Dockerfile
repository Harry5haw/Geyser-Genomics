# Stage 1: Base Image
FROM python:3.11-slim-bullseye

# -----------------------------------------------------------------------------
# System-level dependencies installation
# -----------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    openjdk-11-jre-headless \
    wget \
    unzip \
    bwa \
    samtools \
    bcftools \
    perl  \
    sra-toolkit && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Pre-create the NCBI configuration directory to prevent the interactive first-run wizard.
# The container runs as the 'root' user, so we create it in /root.
RUN mkdir -p /root/.ncbi && chmod -R 777 /root/.ncbi
# ---------------------
# Manually install FastQC.
RUN wget -q https://www.bioinformatics.babraham.ac.uk/projects/fastqc/fastqc_v0.12.1.zip -O /tmp/fastqc.zip && \
    unzip /tmp/fastqc.zip -d /opt/ && \
    rm /tmp/fastqc.zip && \
    chmod +x /opt/FastQC/fastqc && \
    ln -s /opt/FastQC/fastqc /usr/local/bin/fastqc

# -----------------------------------------------------------------------------
# Python application setup
# -----------------------------------------------------------------------------
WORKDIR /app

# FIX: Changed 'app/requirements.txt' to 'requirements.txt'
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# FIX: Changed 'app/tasks.py' to 'tasks.py'
COPY tasks.py .

# -----------------------------------------------------------------------------
# Entrypoint Configuration
# -----------------------------------------------------------------------------
ENTRYPOINT ["python", "tasks.py"]
CMD ["--help"]
