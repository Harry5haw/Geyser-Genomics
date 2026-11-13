"""W2, the static publisher: `manage.py publish` renders the live UI's pages to a folder or an S3 bucket.

The published folder is served as it is (http.server here, S3 + CloudFront when hosted), and must show what the
Django pages show; every link in it must resolve inside it; only allowlisted outputs are published; no inline
style attributes (the hosted CSP has no 'unsafe-hashes'); every inline script/style hash is in csp-hashes.json.
"""

import functools
import html
import http.server
import json
import re
import threading
import urllib.request
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

from samples import publish as publish_mod
from samples.alphagenome import TERMS_URL
from samples.context_processors import GITHUB_URL
from samples.publish import HTML_CACHE, IMMUTABLE, S3Writer, csp_hashes
from samples.views import BUILT_ON

from .web_helpers import FASTP_SINGLE, fastq_gz, gz, put

HEADER = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
LINK = re.compile(r'\s(?:href|src)="([^"]+)"')
STATS = {
    "stats/S1/flagstat.txt": b"100 + 0 in total\n",
    "stats/S1/mosdepth.summary.txt": b"chrom\tlength\tbases\tmean\tmin\tmax\ntotal\t10\t300\t30.0\t0\t60\n",
}


def finished_sample(storage, sample_id="S1", calls=120):
    put(storage, f"raw_reads/{sample_id}.fastq.gz", fastq_gz())
    put(storage, f"qc_reports/{sample_id}.fastp.html", b"<html><body>fastp</body></html>")
    put(storage, f"qc_reports/{sample_id}.fastp.json", json.dumps(FASTP_SINGLE))
    put(storage, f"alignments/{sample_id}.cram", b"CRAM-not-public")
    put(storage, f"alignments/{sample_id}.cram.crai", b"CRAI-not-public")
    records = "".join(f"synth1\t{1000 + 37 * i}\t.\tA\tG\t99.5\t.\tDP=30\tGT\t0/1\n" for i in range(calls))
    put(storage, f"variants/{sample_id}.vcf.gz", gz((HEADER + records).encode()))
    put(storage, f"variants/{sample_id}.vcf.gz.tbi", b"TBI")
    for key, data in STATS.items():
        put(storage, key.replace("S1", sample_id), data)


def running_sample(storage, sample_id="R1"):
    put(storage, f"raw_reads/{sample_id}.fastq.gz", fastq_gz())  # uploaded, nothing else yet: in progress


def run_publish(out, *args):
    call_command("publish", *args, "--out", str(out))
    return Path(out)


def pages(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*.html")}


def resolve(root: Path, link: str):
    """The file a static server returns for a site-relative link (a folder serves its index.html)."""
    path = link.split("#", 1)[0].split("?", 1)[0]
    target = root / path.lstrip("/")
    return target / "index.html" if path.endswith("/") else target


FILES = re.compile(r"<h2>Files</h2>.*?</section>", re.S)


def visible_text(body: str) -> str:
    """The page as a reader sees it, without its Files list (the static one is the public subset: tested apart):
    no tags, scripts or styles, whitespace collapsed."""
    main = FILES.sub("", body.split("<main", 1)[1].split("</main>", 1)[0])
    main = re.sub(r"<(script|style)\b.*?</\1>", "", main, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", main)).split())


@pytest.fixture
def site(storage, tmp_path, settings):
    settings.GEYSER_TABLE_ROWS = 50
    settings.GEYSER_FIGURES_DIR = tmp_path / "no-figures-here"
    finished_sample(storage)
    return tmp_path / "site"


# ------------------------------------------------------------------ what is written


def test_publish_all_writes_the_pages_assets_and_csp_hashes(site):
    root = run_publish(site, "--all")
    for page in ("index.html", "404.html", "samples/S1/index.html", "samples/S1/calls/2/index.html"):
        assert (root / page).is_file(), page
    assert (root / "samples/S1/calls/3/index.html").is_file() and not (root / "samples/S1/calls/4").exists()
    assets = sorted(p.name for p in (root / "static").rglob("*") if p.is_file())
    assert any(re.fullmatch(r"geyser\.[0-9a-f]{12}\.js", a) for a in assets)
    assert not any(a.endswith((".gz", ".br")) for a in assets)  # no precompressed twins
    assert set(json.loads((root / "csp-hashes.json").read_text())) == {"script", "style"}


def test_only_allowlisted_outputs_are_published(site):
    root = run_publish(site, "--all")
    files = sorted(p.name for p in (root / "samples/S1/files").iterdir())
    assert {"S1.vcf.gz", "S1.vcf.gz.tbi", "S1.fastp.html", "flagstat.txt", "mosdepth.summary.txt"} <= set(files)
    assert not any(f.endswith((".fastq.gz", ".cram", ".crai", ".bam")) for f in files)
    everything = b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())
    assert b"CRAM-not-public" not in everything and b"CRAI-not-public" not in everything
    detail = (root / "samples/S1/index.html").read_text(encoding="utf-8")
    assert "S1.cram" not in detail and "raw_reads" not in detail and "S1.fastq.gz" not in detail


# The only links that leave the site: the code (header and footer), the AlphaGenome output terms (footer) and the
# tools How it works credits (views.BUILT_ON).
EXTERNAL = (GITHUB_URL, TERMS_URL, *(url for _, url, _ in BUILT_ON))


def test_every_link_in_the_published_folder_resolves(site):
    root = run_publish(site, "--all")
    for name, body in pages(root).items():
        for link in LINK.findall(body):
            link = html.unescape(link)
            if link.startswith(("#", "mailto:", *EXTERNAL)):
                continue
            assert link.startswith("/"), f"{name}: {link} leaves the site"
            assert resolve(root, link).is_file(), f"{name}: {link} is not in the folder"


def test_no_inline_style_attributes_and_every_inline_block_is_hashed(site):
    root = run_publish(site, "--all")
    published = pages(root)
    for name, body in published.items():
        assert not re.search(r"\sstyle=", body), f"{name} has a style attribute (the CSP forbids them)"
    assert json.loads((root / "csp-hashes.json").read_text()) == csp_hashes(published)
    assert csp_hashes(published)["script"]  # the speculation rules at least


def test_a_one_sample_publish_keeps_every_page_types_csp_hashes(storage, site, tmp_path):
    whole = json.loads((run_publish(tmp_path / "whole", "--all") / "csp-hashes.json").read_text())
    one = json.loads((run_publish(site, "S1") / "csp-hashes.json").read_text())
    # core (404, How it works); +samples (list, upload); +samples+progress (detail); +figures. How it works' own CSS
    # is a stylesheet (about.css), so it adds no hash: every hash is in every response's CSP header.
    assert one == whole and len(one["style"]) == 4


def test_the_call_pages_stop_at_max_pages_and_point_at_the_vcf(storage, site):
    finished_sample(storage, "BIG", calls=1500)
    root = run_publish(site, "BIG", "--max-pages", "3")
    assert (root / "samples/BIG/calls/3/index.html").is_file() and not (root / "samples/BIG/calls/4").exists()
    last = (root / "samples/BIG/calls/3/index.html").read_text(encoding="utf-8")
    assert 'rel="next"' not in last and 'rel="prev"' in last
    assert "The first 150 calls are on these pages; the VCF below has all 1500." in last


def test_one_sample_publishes_its_pages_and_the_list_only(storage, site):
    finished_sample(storage, "S2")
    root = run_publish(site, "S2")
    assert (root / "samples/S2/index.html").is_file() and (root / "index.html").is_file()
    assert not (root / "samples/S1").exists()
    assert (root / "figures/index.html").is_file()  # every page links to it, so every publish writes it
    assert 'href="/samples/S1/"' in (root / "index.html").read_text(encoding="utf-8")  # the list has every sample


def test_a_running_sample_has_a_row_but_no_page_and_no_link(storage, site):
    running_sample(storage)
    root = run_publish(site, "--all")
    index = (root / "index.html").read_text(encoding="utf-8")
    assert "R1" in index and 'href="/samples/R1/"' not in index
    assert "data-status-api" not in index  # no API configured: nothing to poll
    assert not (root / "samples/R1").exists()


def test_figures_are_published_with_one_sample_when_their_csvs_are_here(storage, tmp_path, settings):
    from django.conf import settings as live

    settings.GEYSER_FIGURES_DIR = Path(live.REPO_ROOT) / "docs" / "figures" / "csv"
    if not settings.GEYSER_FIGURES_DIR.is_dir():
        pytest.skip("no figure CSVs in this checkout")
    finished_sample(storage)
    root = run_publish(tmp_path / "site", "S1")
    assert (root / "figures/index.html").is_file()
    assert sorted(p.suffix for p in (root / "figures").iterdir() if p.stem == "coverage") == [".csv", ".json"]


# ------------------------------------------------------------------ the web API (W3, #213)


def test_with_an_api_base_files_come_from_the_api_and_running_rows_poll_it(storage, site):
    running_sample(storage)
    api = "https://api.example.test"
    root = run_publish(site, "--all", "--api-base", api + "/")
    assert not (root / "samples/S1/files").exists()  # the API's presigned links serve them
    detail = (root / "samples/S1/index.html").read_text(encoding="utf-8")
    assert f'href="{api}/files/S1/vcf"' in detail and f'href="{api}/files/S1/qc_html"' in detail
    assert f"{api}/files/S1/cram" not in detail  # token files are not on the public page
    index = (root / "index.html").read_text(encoding="utf-8")
    assert f'data-status-api="{api}/status/R1"' in index


# ------------------------------------------------------------------ the same as the Django pages


@pytest.fixture
def served(site):
    """The published folder over HTTP, as a static host serves it (a folder answers with its index.html)."""
    root = run_publish(site, "--all")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fetch(url: str) -> str:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - our own local server
        return response.read().decode("utf-8")


@pytest.mark.parametrize("path", ["/samples/S1/", "/samples/S1/calls/2/", "/samples/S1/calls/3/"])
def test_a_served_page_reads_the_same_as_the_django_page(client, served, path):
    assert visible_text(fetch(served + path)) == visible_text(client.get(path).content.decode())


def file_labels(body: str) -> list:
    return re.findall(r"<li><a [^>]*download>([^<]+)</a>", FILES.search(body).group())


def test_the_served_files_are_the_django_pages_public_ones(client, served):
    static, live = fetch(served + "/samples/S1/"), client.get("/samples/S1/").content.decode()
    hidden = {"Raw reads", "Alignments", "Alignment index"}
    assert hidden <= set(file_labels(live))
    assert file_labels(static) == [label for label in file_labels(live) if label not in hidden]


def test_the_served_list_shows_the_same_samples_as_the_django_list(client, served):
    static, live = fetch(served + "/"), client.get("/").content.decode()
    table = re.compile(r'<div id="sample-table".*?</table>', re.S)
    rows = re.compile(r"<tr>.*?</tr>|<tr .*?</tr>", re.S)
    strip = re.compile(r'<td class="muted">.*?</td>', re.S)  # "Last activity": absolute when static
    assert [strip.sub("", r) for r in rows.findall(table.search(static).group())] == [
        strip.sub("", r) for r in rows.findall(table.search(live).group())
    ]
    assert "<form" in live and "<form" not in static


def test_a_served_download_is_the_stored_file(served, storage):
    with urllib.request.urlopen(served + "/samples/S1/files/S1.vcf.gz") as response:  # noqa: S310
        assert response.read() == (Path(storage.describe("variants/S1.vcf.gz"))).read_bytes()


# ------------------------------------------------------------------ S3, caching, errors


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kw):
        self.objects[kw["Key"]] = kw

    def upload_fileobj(self, source, bucket, key, ExtraArgs):  # noqa: N803 - boto3's name
        self.objects[key] = {"Key": key, "Body": source.read(), "streamed": True, **ExtraArgs}


def test_s3_objects_get_their_type_and_cache_lifetime(site, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(publish_mod, "writer_for", lambda out: S3Writer("site-bucket", "pre/", client=fake))
    invalidations = []
    monkeypatch.setattr(publish_mod, "_invalidate", lambda dist, paths: invalidations.append((dist, paths)))
    monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E123")
    call_command("publish", "S1", "--out", "s3://site-bucket/pre")
    page = fake.objects["pre/samples/S1/index.html"]
    assert page["ContentType"].startswith("text/html") and page["CacheControl"] == HTML_CACHE
    (js,) = [o for k, o in fake.objects.items() if re.search(r"/geyser\.[0-9a-f]{12}\.js$", k)]
    assert js["CacheControl"] == IMMUTABLE and js["ContentType"].startswith("text/javascript")
    assert list(fake.objects)[-1] == "pre/index.html"  # the list last, after the pages it links to
    vcf = fake.objects["pre/samples/S1/files/S1.vcf.gz"]
    assert vcf["streamed"] and vcf["ContentType"] == "application/gzip"  # big files: multipart, not in memory
    assert invalidations == [
        ("E123", ["/", "/index.html", "/404.html", "/figures/*", "/about/*", "/about.css", "/samples/S1/*"])
    ]


def test_out_defaults_to_the_site_bucket(site, monkeypatch):
    seen = []
    monkeypatch.setattr(publish_mod, "writer_for", lambda out: seen.append(out) or publish_mod.FolderWriter(str(site)))
    monkeypatch.setenv("SITE_BUCKET", "my-site")
    call_command("publish", "S1")
    assert seen == ["s3://my-site"]


@pytest.mark.parametrize(
    "args, message",
    [((), "name the samples"), (("NOPE", "--out", "x"), "not a sample"), (("S1",), "no output")],
)
def test_a_publish_that_cant_go_ahead_says_why(site, monkeypatch, args, message):
    monkeypatch.delenv("SITE_BUCKET", raising=False)
    with pytest.raises(CommandError, match=message):
        call_command("publish", *args)


# ------------------------------------------------------------------ status of a cloud run


def test_a_cloud_run_closed_as_failed_shows_failed_not_running(storage, client):
    put(storage, "raw_reads/F1.fastq.gz", fastq_gz())
    part = {"task": "Finalize", "status": "failed", "run_name": "F1-run", "error": {"stage": "Align"}}
    put(storage, "runs/F1.provenance/Finalize.json", json.dumps(part))
    body = client.get("/").content.decode()
    row = re.search(r"<tr[^>]*>\s*<td class=\"id\"><a href=\"/samples/F1/\">.*?</tr>", body, re.S).group()
    assert 'class="badge failed"' in row


def test_a_paired_sample_publishes_neither_mate(storage, site):
    from .web_helpers import FASTP_PAIRED

    for mate in ("R1", "R2"):
        put(storage, f"raw_reads/P1_{mate}.fastq.gz", fastq_gz())
    put(storage, "qc_reports/P1.fastp.html", b"<html></html>")
    put(storage, "qc_reports/P1.fastp.json", json.dumps(FASTP_PAIRED))
    put(storage, "alignments/P1.cram", b"CRAM-not-public")
    put(storage, "variants/P1.vcf.gz", gz((HEADER + "synth1\t1000\t.\tA\tG\t99\t.\tDP=30\tGT\t0/1\n").encode()))
    root = run_publish(site, "P1")
    files = sorted(p.name for p in (root / "samples/P1/files").iterdir())
    assert files == ["P1.fastp.html", "P1.fastp.json", "P1.vcf.gz"]
    detail = (root / "samples/P1/index.html").read_text(encoding="utf-8")
    assert "P1_R1.fastq.gz" not in detail and "Raw reads" not in detail


def test_a_local_rerun_of_a_failed_sample_shows_running_not_failed(storage):
    """The record is the last run's until the re-run's first stage writes a part: the worker's state wins."""
    from samples import status as status_mod

    view = status_mod.build_sample(
        "S1", set(), [], {"state": "running", "started": 1.0}, "single", {"status": "failed"}
    )
    assert view.state == "running" and view.active


def test_a_deepvariant_called_sample_links_deepvariants_vcf(storage, tmp_path, settings):
    """The run record makes DeepVariant's VCF the calls: the API link names its own kind, the copy is that file."""
    from .test_web_views import dv_sample

    settings.GEYSER_FIGURES_DIR = tmp_path / "no-figures-here"
    dv_sample(storage)
    api = "https://api.example.test"
    with_api = run_publish(tmp_path / "api", "S1", "--api-base", api).joinpath("samples/S1/index.html")
    detail = with_api.read_text(encoding="utf-8")
    assert f'href="{api}/files/S1/deepvariant_vcf"' in detail and f"{api}/files/S1/vcf" not in detail
    folder = run_publish(tmp_path / "folder", "S1")
    copied = (folder / "samples/S1/files/S1.vcf.gz").read_bytes()
    assert copied == Path(storage.describe("variants_deepvariant/S1.vcf.gz")).read_bytes()


# ------------------------------------------------------------------ the Publish metric (O7)


def _publish_metrics(storage):
    path = Path(storage.root) / "metrics" / "metrics.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [r for r in map(json.loads, lines) if r.get("task") == "Publish"]


def test_a_publish_records_its_metric_beside_local_storage(storage, site, monkeypatch):
    monkeypatch.delenv("METRICS_BACKEND", raising=False)
    monkeypatch.delenv("METRICS_FILE", raising=False)
    finished_sample(storage)
    run_publish(site, "S1")
    run_publish(site / "all", "--all")
    recs = _publish_metrics(storage)
    assert [(r["sample_id"], r["status"]) for r in recs] == [("S1", "Success"), ("all", "Success")]
    assert all(r["duration_seconds"] >= 0 for r in recs)


def test_a_failed_publish_records_failure_and_still_fails(storage, site, monkeypatch):
    monkeypatch.delenv("METRICS_BACKEND", raising=False)
    from samples.management.commands import publish as command

    def broken(**kwargs):
        raise publish_mod.PublishError("site bucket unreachable")

    monkeypatch.setattr(command, "publish", broken)
    with pytest.raises(CommandError, match="site bucket unreachable"):
        run_publish(site, "S1", "S2")
    assert [(r["sample_id"], r["status"]) for r in _publish_metrics(storage)] == [("several", "Failure")]


def test_a_metrics_error_never_fails_a_publish(storage, site, monkeypatch, capsys):
    from samples.management.commands import publish as command

    class Broken:
        def record(self, *args, **kwargs):
            raise RuntimeError("CloudWatch down")

    monkeypatch.setattr(command, "get_metrics", lambda: Broken())
    finished_sample(storage)
    run_publish(site, "S1")  # no exception
    assert "could not record the Publish metric: RuntimeError: CloudWatch down" in capsys.readouterr().err


def test_the_ui_never_picks_cloudwatch_from_local_storage(settings, monkeypatch):
    from geyser_web.pipeline import get_metrics
    from metrics import CloudWatchMetrics, LocalMetrics

    monkeypatch.delenv("METRICS_BACKEND", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")  # the process env says S3, but the UI's settings say local
    settings.GEYSER_STORAGE_BACKEND = "local"
    assert isinstance(get_metrics(), LocalMetrics)
    settings.GEYSER_STORAGE_BACKEND = "s3"
    with mock.patch("boto3.client"):
        assert isinstance(get_metrics(), CloudWatchMetrics)  # the Publish job: S3 storage on AWS


def test_how_it_works_carries_its_css_as_a_stylesheet_not_inline(site):
    root = run_publish(site, "--all")
    css = (root / "about.css").read_text(encoding="utf-8")
    assert ".mdv" in css and ".built" in css and "/*" not in css  # the motifs and its own rules, minified
    about = (root / "about/index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/about.css">' in about
    style = re.compile(r"<style>(.*?)</style>", re.S)
    four_oh_four = (root / "404.html").read_text(encoding="utf-8")
    assert style.search(about).group(1) == style.search(four_oh_four).group(1)  # the same inline block: same hash
