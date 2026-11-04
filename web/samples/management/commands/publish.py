"""manage.py publish: render the UI to a static site (a folder, or the hosted site's S3 bucket).

    python manage.py publish --all --out site/                     # every sample, the figures; a folder
    python manage.py publish SRR062634 --out s3://my-site          # one sample's pages and the list (a run's last step)
    python manage.py publish --all --api-base https://….lambda-url.eu-west-2.on.aws --out s3://my-site

--out defaults to s3://$SITE_BUCKET, --api-base to $GEYSER_API_BASE, the CloudFront invalidation to
$CLOUDFRONT_DISTRIBUTION_ID. Exit code 0: published; 1: not (the reason on stderr).
"""

import os

from django.core.management.base import BaseCommand, CommandError

from samples.publish import PublishError, publish


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
        except PublishError as err:
            raise CommandError(str(err)) from err
