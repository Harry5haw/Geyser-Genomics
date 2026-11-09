"""manage.py publish: render the UI to a static site (a folder, or the hosted site's S3 bucket).

    python manage.py publish --all --out site/                     # every sample, the figures; a folder
    python manage.py publish SRR062634 --out s3://my-site          # one sample's pages and the list (a run's last step)
    python manage.py publish --all --api-base https://….lambda-url.eu-west-2.on.aws --out s3://my-site

--out defaults to s3://$SITE_BUCKET, --api-base to $GEYSER_API_BASE, the CloudFront invalidation to
$CLOUDFRONT_DISTRIBUTION_ID. Exit code 0: published; 1: not (the reason on stderr).

Every publish records a Publish metric through app/metrics.py, like a pipeline stage: Duration (TaskName
Publish, SampleId the one sample, else "all" or "several", Status) and FailureCount on failure. In the
Publish job (STORAGE_BACKEND=s3) that is CloudWatch's GeyserGenomics namespace, which the dashboard's stage
groups read (O7). Best effort: a metrics error is a warning and never changes the exit code.
"""

import os
import time

from django.core.management.base import BaseCommand, CommandError

from geyser_web.pipeline import get_metrics
from samples.publish import PublishError, publish

METRIC_TASK = "Publish"


def metric_sample_id(sample_ids, everything) -> str:
    """The SampleId the metric carries: the run's sample for a per-run publish (the Publish job's case)."""
    if everything:
        return "all"
    return sample_ids[0] if len(sample_ids) == 1 else "several"


class Command(BaseCommand):
    help = "Render the UI as a static site: the sample list, sample pages, figures, assets and CSP hashes."

    def add_arguments(self, parser):
        parser.add_argument("sample_ids", nargs="*", help="samples to (re)publish, with the list")
        parser.add_argument("--all", action="store_true", help="every sample, and the figures if their CSVs are here")
        parser.add_argument("--out", default="", help="a folder, or s3://bucket[/prefix] (default s3://$SITE_BUCKET)")
        parser.add_argument("--api-base", default=None, help="the web API's origin (default $GEYSER_API_BASE)")
        parser.add_argument("--max-pages", type=int, default=20, help="call pages per sample (50 calls each)")

    def handle(self, *args, **opts):
        if not opts["all"] and not opts["sample_ids"]:
            raise CommandError("name the samples to publish, or --all")
        bucket = os.environ.get("SITE_BUCKET", "").strip()
        out = opts["out"] or (f"s3://{bucket}" if bucket else "")
        api_base = opts["api_base"] if opts["api_base"] is not None else os.environ.get("GEYSER_API_BASE", "")
        start, status = time.time(), "Failure"
        try:
            publish(
                sample_ids=opts["sample_ids"],
                everything=opts["all"],
                out=out,
                api_base=api_base,
                max_pages=opts["max_pages"],
                distribution_id=os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", "").strip(),
                log=lambda message: self.stdout.write(message),
            )
            status = "Success"
        except PublishError as err:
            raise CommandError(str(err)) from err
        finally:
            self._record(metric_sample_id(opts["sample_ids"], opts["all"]), status, time.time() - start)

    def _record(self, sample_id, status, duration):
        try:
            get_metrics().record(METRIC_TASK, sample_id, status, duration)
        except Exception as err:  # noqa: BLE001 - metrics are best effort: never fail (or un-fail) a publish
            self.stderr.write(f"WARNING: could not record the {METRIC_TASK} metric: {type(err).__name__}: {err}")
