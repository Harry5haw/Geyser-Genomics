"""Publish the UI as a static site (web plan W2): `manage.py publish [--all | <id> ...] --out DIR|s3://bucket`.

Every page is the live UI's own view and template, rendered with a StaticSite on the request
(samples/static_site.py), so the published pages and the Django pages can't drift apart:
- index.html (the sample list), samples/<id>/index.html and samples/<id>/calls/<n>/index.html (the call pages,
  up to max_pages per sample: past that the VCF has the rest), figures/, about/ and about.css (every publish, so every page's link to
  them resolves; each figure's data when its CSV is here: they live in the repo, not the data lake), 404.html, and with an API base uploads/index.html (the
  token-gated upload page: the browser sends the reads to the data lake through the API);
- the static files under static/ with their content-hashed names, as collectstatic writes them;
- csp-hashes.json: the sha256 of every inline <script> and <style>, for the hosted site's strict CSP;
- with no API base, the public files themselves under samples/<id>/files/ (the allowlist only; never reads or
  alignments), so a published folder works on its own.

Clean URLs: links are folders (/samples/S1/), served as their index.html by any static server and by the hosted
site's CloudFront function. Caching: hashed assets are immutable for a year; HTML and data are short-lived.
Order: assets and data first, pages next, the list last, so no published page names a file not yet there.

A site starts with `publish --all`; after that each run publishes its own sample (the list links every finished
sample, so one never published would be a 404). Links are root-absolute: with `--out s3://bucket/prefix`, the
CDN's origin path must be that prefix. After an image whose inline CSS or scripts changed, publish --all again
(every page carries the new blocks) and update the CSP from the new csp-hashes.json.
"""

import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import tempfile

from django.conf import settings
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.urls import reverse

from geyser_web.pipeline import get_storage

from . import figures as figures_mod
from . import status as status_mod
from . import views
from .static_site import PUBLIC_KINDS, StaticSite, reference_names
from .vcf import read_truth, read_variants

IMMUTABLE = "public, max-age=31536000, immutable"  # content-hashed names: a change is a new name
HTML_CACHE = "public, max-age=60"  # pages change when a run publishes; a minute at most, and invalidated
DATA_CACHE = "public, max-age=300"
HASHED_NAME = re.compile(r"\.[0-9a-f]{12}\.[A-Za-z0-9]+$")  # ManifestStaticFilesStorage's geyser.<hash>.js
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
INLINE_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".gz": "application/gzip",
    ".tbi": "application/octet-stream",
}


class PublishError(Exception):
    """A publish that can't go ahead (unknown sample, bad --out): a message for the operator, exit code 1."""


def content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class FolderWriter:
    """A local folder: `python -m http.server -d DIR` serves it as the site."""

    def __init__(self, root: str):
        self.root = root
        self.written = []

    def put(self, path: str, data: bytes, cache_control: str) -> None:
        full = os.path.join(self.root, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
        self.written.append(path)

    def put_stream(self, path: str, source, cache_control: str) -> None:
        full = os.path.join(self.root, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            shutil.copyfileobj(source, fh, 1024 * 1024)
        self.written.append(path)

    def describe(self) -> str:
        return self.root


class S3Writer:
    """The hosted site's bucket (CloudFront in front): each object with its content type and Cache-Control."""

    def __init__(self, bucket: str, prefix: str = "", client=None):
        if client is None:
            import boto3  # only when publishing to S3

            client = boto3.client("s3")
        self.bucket, self.prefix, self.client = bucket, prefix, client
        self.written = []

    def put(self, path: str, data: bytes, cache_control: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.prefix + path,
            Body=data,
            ContentType=content_type(path),
            CacheControl=cache_control,
        )
        self.written.append(path)

    def put_stream(self, path: str, source, cache_control: str) -> None:
        """A large file (a whole-genome VCF is hundreds of MB): multipart in parts, never whole in memory."""
        extra = {"ContentType": content_type(path), "CacheControl": cache_control}
        self.client.upload_fileobj(source, self.bucket, self.prefix + path, ExtraArgs=extra)
        self.written.append(path)

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


def writer_for(out: str):
    if not out:
        raise PublishError("no output: give --out DIR or s3://bucket[/prefix] (or set SITE_BUCKET)")
    if out.startswith("s3://"):
        bucket, _, prefix = out[len("s3://") :].partition("/")
        if not bucket:
            raise PublishError(f"no bucket in {out!r}")
        prefix = prefix.strip("/")
        return S3Writer(bucket, prefix + "/" if prefix else "")
    return FolderWriter(out)


def csp_hashes(pages: dict) -> dict:
    """'sha256-…' sources for every distinct inline script and style, for the CSP's script-src / style-src."""

    def digest(text: str) -> str:
        return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")

    scripts, styles = set(), set()
    for html in pages.values():
        scripts.update(digest(m) for m in INLINE_SCRIPT.findall(html))
        styles.update(digest(m) for m in INLINE_STYLE.findall(html))
    return {"script": sorted(scripts), "style": sorted(styles)}


def _render(view, path: str, site: StaticSite, **kwargs) -> str:
    request = RequestFactory().get(path)
    request.geyser_static = site
    response = view(request, **kwargs)
    if response.status_code != 200:
        raise PublishError(f"{path}: the page answered {response.status_code}")
    return response.content.decode("utf-8")


def _copy_public_files(storage, writer, sample, site) -> int:
    """With no API: the allowlisted files that exist, next to the sample's pages (as the page links them)."""
    copied = 0
    for kind in PUBLIC_KINDS:
        key = views._file_key(sample.sample_id, kind, sample.keys)
        if not key or not storage.exists(key):
            continue
        with storage.open_read(key) as fh:  # streamed: the 1 GiB job can't hold a whole-genome VCF twice
            writer.put_stream(f"samples/{sample.sample_id}/files/{os.path.basename(key)}", fh, DATA_CACHE)
        copied += 1
    return copied


def publish(sample_ids=(), everything=False, out="", api_base="", max_pages=20, distribution_id="", log=print):
    """Render and write the site. Returns a summary: what was written, and the CSP hashes."""
    writer = writer_for(out)
    try:
        storage = get_storage()
    except RuntimeError as err:  # storage_from_env's configuration errors, e.g. no BUCKET_NAME with s3
        raise PublishError(str(err)) from err
    site = StaticSite(api_base=(api_base or "").rstrip("/"), max_pages=max(1, int(max_pages)))
    # One listing for the whole site: the list, every row and every sample page come from it.
    site.samples = {s.sample_id: s for s in status_mod.load_all(storage)}
    site.references = reference_names(storage.list_keys("reference/"))  # the upload page's reference choice
    unknown = [i for i in sample_ids if i not in site.samples]
    if unknown:
        raise PublishError(f"not a sample in {storage.describe('raw_reads/')}: {', '.join(unknown)}")
    targets = list(site.samples) if everything else list(dict.fromkeys(sample_ids))
    size = max(1, settings.GEYSER_TABLE_ROWS)

    with (
        tempfile.TemporaryDirectory(prefix="geyser-static-") as static_root,
        override_settings(
            DEBUG=False,
            STATIC_ROOT=static_root,
            STORAGES={"staticfiles": {"BACKEND": "geyser_web.static_storage.ForgivingManifestStaticFilesStorage"}},
        ),
    ):
        call_command("collectstatic", interactive=False, verbosity=0)
        # 1. Assets: the hashed copies only (the pages name those), without the precompressed twins: the CDN
        # compresses, and a local folder doesn't need them.
        assets = 0
        for dirpath, _, filenames in os.walk(static_root):
            for name in filenames:
                if not HASHED_NAME.search(name):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, static_root).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    writer.put(settings.STATIC_URL.strip("/") + "/" + rel, fh.read(), IMMUTABLE)
                assets += 1

        pages = {}
        for sample_id in targets:
            sample = site.samples[sample_id]
            if sample.active:
                log(f"{sample_id}: still running, so no page yet (its row on the list says so)")
                continue
            # 2. The sample's data: one pass over its VCF for every call page.
            key = views.variants_key_of(storage, sample)
            truth = read_truth(storage, sample_id)
            table = read_variants(storage, key, site.max_pages * size, 0, want_calls=truth is not None)
            if table is not None:
                site.variants[key] = table
            if not site.api_base:
                _copy_public_files(storage, writer, sample, site)
            pages[f"samples/{sample_id}/index.html"] = _render(
                views.sample_detail, reverse("samples:detail", args=[sample_id]), site, sample_id=sample_id
            )
            n_pages = min(site.max_pages, math.ceil(table.total / size)) if table and table.total else 1
            for page in range(2, n_pages + 1):
                path = reverse("samples:detail_page", args=[sample_id, page])
                pages[f"samples/{sample_id}/calls/{page}/index.html"] = _render(
                    views.sample_detail, path, site, sample_id=sample_id, page=page
                )

        # 3. The figures, with every publish: each grid's JSON and CSV when its CSV is here (they live in the repo, not
        # the data lake), and the page always. Every page links to it (footer, list, 404), and the publish job can't
        # read the bucket to see whether an earlier --all wrote it, so a site that only ever had one-sample publishes
        # would otherwise have a dead link. A missing figure says so on its card.
        for name in figures_mod.FIGURES:
            try:
                grid = figures_mod.load(name)
            except Exception as err:  # noqa: BLE001 - its card says so; the other figures still publish
                log(f"figure {name}: not published ({type(err).__name__})")
                continue
            if grid is None:
                continue
            writer.put(f"figures/{name}.json", figures_mod.grid_json(grid).encode("utf-8"), DATA_CACHE)
            with open(figures_mod.csv_path(grid.figure), "rb") as fh:
                writer.put(f"figures/{name}.csv", fh.read(), DATA_CACHE)
        pages["figures/index.html"] = _render(views.figures_page, reverse("samples:figures"), site)
        # Every page's header links to it, so every publish writes it (as with the figures).
        pages["about/index.html"] = _render(views.about_page, reverse("samples:about"), site)
        writer.put(
            "about.css", _render(views.about_css, reverse("samples:about_css"), site).encode("utf-8"), HTML_CACHE
        )

        request = RequestFactory().get("/404.html")
        request.geyser_static = site
        pages["404.html"] = render_to_string("samples/404.html", {"static": site}, request=request)
        pages["index.html"] = _render(views.sample_list, reverse("samples:list"), site)
        if site.api_base:  # the upload page talks to the web API: without one there is nothing to upload to
            pages["uploads/index.html"] = _render(views.upload_page, reverse("samples:upload_page"), site)

    hashes = csp_hashes(pages)
    writer.put("csp-hashes.json", json.dumps(hashes, indent=1).encode("utf-8"), DATA_CACHE)
    # 4. Pages last, the list after the sample pages it links to.
    for path in sorted(pages, key=lambda p: (p == "index.html", p)):
        writer.put(path, pages[path].encode("utf-8"), HTML_CACHE)

    invalidated = []
    if distribution_id and isinstance(writer, S3Writer):
        changed = [
            "/",
            "/index.html",
            "/404.html",
            "/figures/*",
            "/about/*",
            "/about.css",
            *(f"/samples/{i}/*" for i in targets),
        ]
        if site.api_base:
            changed.append("/uploads/*")  # it lists the IDs on the site (to ask before replacing one)
        invalidated = ["/*"] if everything else changed
        _invalidate(distribution_id, invalidated)
    summary = {
        "out": writer.describe(),
        "pages": len(pages),
        "assets": assets,
        "samples": [i for i in targets if not site.samples[i].active],
        "figures": True,
        "csp": hashes,
        "invalidated": invalidated,
    }
    log(
        f"published {summary['pages']} pages, {assets} assets, {len(summary['samples'])} sample(s) "
        f"to {summary['out']}" + (f"; invalidated {', '.join(invalidated)}" if invalidated else "")
    )
    return summary


def _invalidate(distribution_id: str, paths: list) -> None:
    """Ask CloudFront to drop its copies of the changed pages (HTML is short-lived anyway: this makes it now)."""
    import time

    import boto3

    boto3.client("cloudfront").create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": f"geyser-publish-{time.time_ns()}",
        },
    )
