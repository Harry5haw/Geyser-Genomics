# Start from the official Airflow image
FROM apache/airflow:2.8.1

# Switch to the root user to install things
USER root

# Install FastQC and its Java dependency
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    msopenjdk-11 \
    wget \
    unzip \
    libfreetype6 \
    fontconfig \
    bwa \
    samtools \
    bcftools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
RUN wget -q https://www.bioinformatics.babraham.ac.uk/projects/fastqc/fastqc_v0.12.1.zip -O /tmp/fastqc.zip && \
    unzip /tmp/fastqc.zip -d /opt/ && \
    rm /tmp/fastqc.zip && \
    chmod +x /opt/FastQC/fastqc && \
    ln -s /opt/FastQC/fastqc /usr/local/bin/fastqc

# Create a directory for our requirements
RUN mkdir -p /opt/airflow/requirements

# Copy our local requirements.txt file into the container
COPY requirements.txt /opt/airflow/requirements/

# Switch back to the airflow user
USER airflow

# Install the packages from our requirements.txt file
RUN pip install --no-cache-dir -r /opt/airflow/requirements/requirements.txt