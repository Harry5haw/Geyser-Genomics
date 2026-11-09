# app-annotate/Dockerfile
#
# The AlphaGenome annotation stage (3.0, #207): geyser-app plus the alphagenome client, for
# `tasks.py annotate --source atlas`. Everything else (tasks.py, bcftools for the annotated VCF, Python) is
# geyser-app's, so the two images can't drift apart. Build geyser-app first, from the repo root:
#
#   docker build -f Dockerfile -t geyser-app .
#   docker build -f app-annotate/Dockerfile -t geyser-annotate .        # BASE_IMAGE=geyser-app by default
#
# Run (the cloud: the Annotate job definition, infrastructure/alphagenome.tf):
#   docker run --rm -e ALPHAGENOME_API_KEY ... geyser-annotate python tasks.py annotate <id> <reference_name>
#
# Separate from geyser-app because the client and its dependencies (pandas, scipy, matplotlib, grpcio, anndata)
# are ~500 MB, which every other stage would pull for a step only this job runs. The key is never in the image:
# the job gets ALPHAGENOME_API_KEY at run time (in the cloud, from the SSM SecureString via the job definition).

ARG BASE_IMAGE=geyser-app
FROM ${BASE_IMAGE}

COPY requirements-alphagenome.txt /tmp/requirements-alphagenome.txt
RUN pip install --no-cache-dir -r /tmp/requirements-alphagenome.txt && rm /tmp/requirements-alphagenome.txt

# The cloud runs the Atlas only (on S3 storage anything else is a usage error); a job can still override it.
ENV ALPHAGENOME_SOURCE=atlas
# The client must import here, or the stage would only find out at its first variant.
RUN python -c "from alphagenome.atlas import atlas; from alphagenome.data import genome; import alphagenome_annotation"
