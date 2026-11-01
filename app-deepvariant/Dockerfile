# app-deepvariant/Dockerfile
#
# DeepVariant variant-calling stage. Kept separate from geyser-app: the GPU base image alone is ~10 GB.
# Build from the repo root (it copies the shared storage/metrics code from app/):
#
#   docker build -f app-deepvariant/Dockerfile -t geyser-deepvariant .                                  # GPU
#   docker build -f app-deepvariant/Dockerfile --build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu .   # CPU
#
# Run (normally via scripts/run_local.py --caller deepvariant):
#   docker run --rm --gpus all ... geyser-deepvariant /opt/geyser-venv/bin/python deepvariant_task.py <id> <ref>

ARG DV_TAG=1.10.0-gpu
FROM google/deepvariant:${DV_TAG}

USER root

# The base (Ubuntu 22.04, Python 3.10) already ships samtools 1.15.1 for BAM indexing and faidx.
# python3-venv is missing (no ensurepip), and we need it for a separate env for our code (below).
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv && \
    rm -rf /var/lib/apt/lists/* && \
    samtools --version | head -1

# Our code gets its own venv so boto3 can never disturb DeepVariant's pinned Python/TensorFlow packages.
# boto3 is only imported for S3 storage / CloudWatch metrics; local runs never touch it.
RUN python3 -m venv /opt/geyser-venv && \
    /opt/geyser-venv/bin/pip install --no-cache-dir boto3

WORKDIR /app
COPY app/storage.py app/metrics.py app/tasks.py ./
COPY app-deepvariant/deepvariant_task.py ./

ENTRYPOINT []
CMD ["/opt/geyser-venv/bin/python", "deepvariant_task.py", "--help"]
