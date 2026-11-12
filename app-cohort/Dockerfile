# Cohort QC runner: Python 3.11 + Java 11 + Hail 0.2.139 / PySpark 3.5.4 (requirements-cohort.txt), for running
# scripts/cohort_qc.py and scripts/cohort_runs.py (and their tests) on machines where Hail has no build (Windows).
# Not a pipeline image: nothing pushes it to ECR. The repository is mounted at /work at run time
# (docs/cohort-qc.md), so the image only holds the dependencies.
#
#   docker build -f app-cohort/Dockerfile -t geyser-cohort:local .
#   docker run --rm -v "$PWD:/work" geyser-cohort:local python -m pytest tests/test_cohort_qc.py tests/test_cohort_runs.py
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

# Java 11: Hail 0.2.139 is built and tested with it (on 17 it warns "not supported"). Debian bookworm only packages 17,
# so the JRE comes from Eclipse Temurin's image. procps because Spark's launch scripts call ps.
COPY --from=eclipse-temurin:11-jre-jammy@sha256:fc9ca245d0e3c125085fd9d12c5cb4fa7aaa68f419ad0f035ef5f75bd37f90e2 \
    /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# requirements-dev.txt includes the others by relative path, so they keep their places.
COPY requirements-dev.txt requirements-cohort.txt requirements-figures.txt /tmp/req/
COPY app/requirements.txt /tmp/req/app/
COPY web/requirements.txt /tmp/req/web/
RUN pip install --no-cache-dir -r /tmp/req/requirements-dev.txt -r /tmp/req/requirements-cohort.txt

# Spark binds to the container's hostname otherwise, which can fail to resolve.
ENV SPARK_LOCAL_IP=127.0.0.1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
