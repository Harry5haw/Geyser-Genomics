# app-publish/Dockerfile
#
# The static publisher (web plan W2, v1.3): geyser-app plus the web UI, to render the site with
#   python manage.py publish <srr_id> --out s3://<site bucket>      (a run's last step, #213's Publish state)
#   python manage.py publish --all --out s3://<site bucket>         (the whole site, figures included)
# The pipeline code (app/: tasks.py's storage keys, provenance.py's run record, storage.py) is geyser-app's own
# /app, so the pages always read the outputs with the same rules the image that wrote them used. Build geyser-app
# first, from the repo root:
#
#   docker build -f Dockerfile -t geyser-app .
#   docker build -f app-publish/Dockerfile -t geyser-publish .           # BASE_IMAGE=geyser-app by default
#
# Run against a local folder (no AWS):
#   docker run --rm -v "$PWD/local_bucket:/data" -v "$PWD/site:/site" -e STORAGE_BACKEND=local \
#     -e LOCAL_STORAGE_DIR=/data geyser-publish python manage.py publish --all --out /site
#
# Environment: BUCKET_NAME (the data lake), SITE_BUCKET (--out's default, s3://$SITE_BUCKET), GEYSER_API_BASE (the
# web API, #213; without it the public files are copied next to the pages), CLOUDFRONT_DISTRIBUTION_ID (invalidate
# the changed pages), AWS_REGION. It reads the data lake's listing and its public outputs only (qc_reports/,
# variants/, stats/, runs/), never raw_reads/ or alignments/ objects.
#
# Separate from geyser-app because every pipeline job pulls geyser-app before it starts (#141/#142): Django and the
# templates are only for this one short job.

ARG BASE_IMAGE=geyser-app
FROM ${BASE_IMAGE}

WORKDIR /geyser

COPY web/requirements.txt web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

# The repo layout the UI expects (geyser_web/pipeline.py): app/, scripts/, lambda/trigger/ and web/ side by side,
# with app/ being the base image's own.
RUN ln -s /app /geyser/app
COPY lambda/trigger/index.py lambda/trigger/index.py
COPY scripts/run_local.py scripts/make_test_data.py scripts/
COPY docs/figures/csv/ docs/figures/csv/
COPY web/manage.py web/manage.py
COPY web/geyser_web/ web/geyser_web/
COPY web/samples/ web/samples/
COPY app-publish/entrypoint.sh /usr/local/bin/geyser-publish-entrypoint
RUN chmod +x /usr/local/bin/geyser-publish-entrypoint

ENV DJANGO_DEBUG=0 \
    STORAGE_BACKEND=s3 \
    AWS_REGION=eu-west-2 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /geyser/web
ENTRYPOINT ["geyser-publish-entrypoint"]
CMD ["python", "manage.py", "publish", "--help"]
