# Start from the official Airflow image
FROM apache/airflow:2.8.1

# -----------------------------------------------------------------------------
# System-level dependencies installation
# -----------------------------------------------------------------------------
# Switch to the root user ONLY for installing system-level packages.
USER root

# Install all necessary command-line tools using apt-get.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    msopenjdk-11 \
    wget \
    unzip \
    libfreetype6 \
    fontconfig \
    bwa \
    samtools \
    bcftools \
    sra-toolkit \
    awscli && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Manually install FastQC since it's not in the apt repository.
RUN wget -q https://www.bioinformatics.babraham.ac.uk/projects/fastqc/fastqc_v0.12.1.zip -O /tmp/fastqc.zip && \
    unzip /tmp/fastqc.zip -d /opt/ && \
    rm /tmp/fastqc.zip && \
    chmod +x /opt/FastQC/fastqc && \
    ln -s /opt/FastQC/fastqc /usr/local/bin/fastqc

# -----------------------------------------------------------------------------
# Application code and Python dependencies
# -----------------------------------------------------------------------------

# NEW CORRECTED ORDER:
# 1. Copy the application code and requirements first.
#    The 'airflow' user needs to own these files.
COPY --chown=airflow:airflow requirements.txt /requirements.txt
COPY --chown=airflow:airflow app/ /app/

# 2. NOW, switch to the non-root airflow user.
USER airflow

# 3. NOW, install Python packages as the 'airflow' user.
RUN pip install --no-cache-dir -r /requirements.txt

# 4. Set the working directory for the application.
WORKDIR /app

# -----------------------------------------------------------------------------
# Final setup and default command
# -----------------------------------------------------------------------------
# The default command to run when a container starts.
CMD ["airflow", "standalone"]
