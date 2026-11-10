"""Views. Every one of them only reads or writes pipeline storage keys; none starts a run.

Uploading to raw_reads/ is the trigger, exactly as in the cloud (S3 event -> Lambda). Locally
the worker (manage.py runworker) notices the new key; in the cloud the Lambda does.
"""

import os
import time

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.gzip import gzip_page
from django.views.decorators.http import require_GET, require_POST

from geyser_web.pipeline import (
    DEFAULT_REFERENCE_GENOME,
    MATES,
    PAIRED,
    PER_MATE_KEYS,
    LayoutError,
    ObjectNotFound,
    get_storage,
    keys_for,
    keys_of,
    layout_from_raw,
    read_part,
    record_key,
    sample_vcf_key,
)

from . import alphagenome as alphagenome_mod
from . import figures as figures_mod
from . import progress as progress_mod
from . import status as status_mod
from .demo import DemoError, create_demo_sample
from .forms import UploadForm
from .gate import read_gate
from .qc import read_qc_summary
from .record import load_record, record_view
from .runs import is_valid_sample_id, log_key, read_text
from .static_site import GATE_PROFILES, PUBLIC_KINDS, static_of
from .vcf import check_truth, page_of, planted_positions, read_truth, read_variants, truth_key

# Downloadable files: kind -> (label, description). Anything not listed here is a 404.
# Per-read-file kinds (PER_MATE_KEYS) of a paired-end sample are served as <kind>_R1 / <kind>_R2.
FILE_KINDS = {
    "raw": ("Raw reads", "Gzipped FASTQ as uploaded"),
    "qc_html": ("fastp report", "HTML (its charts load a script from opengene.org, so they need a browser online)"),
    "qc_json": ("fastp data", "JSON: every number in the report"),
    "cram": ("Alignments", "Coordinate-sorted, duplicate-marked CRAM (decode with the same reference)"),
    "crai": ("Alignment index", "CRAM index (.crai), for region queries and genome browsers"),
    "bam": ("Alignments (BAM)", "From a run before CRAM, or imported benchmark data"),
    "vcf": ("Variants", "Gzipped VCF"),
    "tbi": ("Variant index", "Tabix index (.tbi) of the VCF"),
    "csq_vcf": (
        "Annotated variants",
        "The VCF with bcftools csq consequences (INFO/BCSQ), when the reference has gene models",
    ),
    "csq_tbi": ("Annotated variant index", "Tabix index (.tbi) of the annotated VCF"),
    "flagstat": ("Alignment stats", "samtools flagstat: reads, mapped, duplicates, properly paired"),
    "mosdepth_summary": ("Coverage summary", "mosdepth: mean, min and max depth per contig (duplicates excluded)"),
    "mosdepth_dist": ("Coverage distribution", "mosdepth: fraction of bases covered at each depth or more"),
    "bcftools_stats": ("Variant stats", "bcftools stats: SNP and indel counts, Ti/Tv, quality distributions"),
    "gate_verdict": ("Quality gate verdict", "JSON: the verdict, named reasons and every check with its thresholds"),
    "alphagenome_tsv": (
        "Variants of interest (AlphaGenome)",
        "TSV: the variants AlphaGenome scored, ranked by AVI. Research use only, not for clinical use",
    ),
    "alphagenome_vcf": (
        "AlphaGenome-annotated variants",
        "The VCF with AlphaGenome scores (INFO/AG_*). Research use only, not for clinical use",
    ),
    "alphagenome_tbi": ("AlphaGenome-annotated variant index", "Tabix index (.tbi) of the AlphaGenome VCF"),
    "run_record": (
        "Run record",
        "JSON: inputs, reference MD5, tool versions, image, each stage's status, time and resources",
    ),
    "truth": ("Planted SNPs", "Truth file for demo samples (JSON)"),
    "log": ("Run log", "Output of the local pipeline run"),
}
# Kinds that are one file per sample but not keys_for() names.
SAMPLE_FILES = ("truth", "log", "run_record")
# AlphaGenome's outputs (3.0): offered only when the panel shows this run's annotation (AnnotationView.current),
# and only by the local UI: the hosted site's downloads are PUBLIC_KINDS (the web API's allowlist), which leaves
# them out, so AlphaGenome output reaches hosted visitors only as the panel, with its terms notice.
ALPHAGENOME_KINDS = alphagenome_mod.KINDS
LOG_TAIL_LINES = 300


def _split_kind(kind):
    """'raw_R1' -> ('raw', 'R1'); 'bam' -> ('bam', None). None for anything not downloadable."""
    for mate in MATES:
        base = kind[: -len(mate) - 1]
        if kind == f"{base}_{mate}" and base in PER_MATE_KEYS:
            return base, mate
    return (kind, None) if kind in FILE_KINDS else None


def _file_key(sample_id, kind, keys):
    if kind == "truth":
        return truth_key(sample_id)
    if kind == "log":
        return log_key(sample_id)
    if kind == "run_record":
        return record_key(sample_id)
    return keys.get(kind)


def _download_kinds(keys):
    """(kind, label, description) for every file a sample can have, both mates for paired-end."""
    for kind, (label, description) in FILE_KINDS.items():
        if kind in keys or kind in SAMPLE_FILES:
            yield kind, label, description
        elif kind in PER_MATE_KEYS:
            for mate in MATES:
                yield f"{kind}_{mate}", f"{label} ({mate})", description


def _list_context(storage, form, static=None):
    # Publishing: the publisher's one listing (its samples dict keeps load_all's order), no second pass.
    samples = list(static.samples.values()) if static else status_mod.load_all(storage)
    return {
        "form": form,
        "static": static,
        "samples": samples,
        "any_active": any(s.active for s in samples),
        "poll_seconds": settings.GEYSER_POLL_SECONDS,
        "stages": status_mod.STAGES,
        "max_upload_mb": settings.GEYSER_MAX_UPLOAD_MB,
        "max_upload_label": UploadForm.limit_label(),
        "max_upload_bytes": UploadForm.max_bytes(),
    }


@require_GET
@gzip_page  # HTML only: downloads (CRAM, VCF.gz) are served as they are
def sample_list(request):
    storage = get_storage()
    static = static_of(request)
    form = None if static else UploadForm(storage=storage)  # the static showcase has no forms
    return render(request, "samples/list.html", _list_context(storage, form, static))


@require_GET
@gzip_page  # HTML only: downloads (CRAM, VCF.gz) are served as they are
def sample_rows(request):
    storage = get_storage()
    return render(request, "samples/_sample_table.html", _list_context(storage, None))


@require_POST
def upload(request):
    storage = get_storage()
    # Refuse an oversized body before Django reads it (the form re-checks the real size).
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > 2 * UploadForm.max_bytes() + 1024 * 1024:  # up to two files (paired-end)
        messages.error(request, f"That file is larger than the {UploadForm.limit_label()} limit.")
        return redirect("samples:list")

    form = UploadForm(request.POST, request.FILES, storage=storage)
    if not form.is_valid():
        return render(request, "samples/list.html", _list_context(storage, form), status=400)
    sample_id, layout = form.cleaned_data["sample_id"], form.cleaned_data["layout"]
    files = [form.cleaned_data["reads"]] + ([form.cleaned_data["reads_r2"]] if layout == PAIRED else [])
    # R2 first: the trigger starts a paired-end sample once both mates exist, whichever lands last.
    for f, key in reversed(list(zip(files, keys_of(keys_for(sample_id, layout), "raw"), strict=True))):
        f.seek(0)
        storage.upload_stream(f, key)
    kind = "paired-end reads" if layout == PAIRED else "reads"
    messages.success(request, f"Uploaded {sample_id} ({kind}). The pipeline starts as soon as the trigger sees it.")
    return redirect("samples:detail", sample_id=sample_id)


@require_GET
@gzip_page
def upload_page(request):
    """The hosted site's upload page: the browser sends the reads straight to the data lake through the web API
    (static/samples/upload.js). Only on the static site with an API; the live UI uploads through its own form."""
    static = static_of(request)
    if not static or not static.api_base:
        raise Http404("Uploads here go through the form on the sample list")
    context = {
        "api_base": static.api_base,
        "existing": sorted(static.samples),  # IDs already on the site: the page asks before replacing one
        "gate_profiles": GATE_PROFILES,
        "references": static.references,
        "default_reference": DEFAULT_REFERENCE_GENOME,
    }
    return render(request, "samples/upload_page.html", context)


@require_GET
@gzip_page
def figures_page(request):
    """The signature figures as 3D terrains: each card shows its inline-SVG poster; the renderer and the grid load
    in the browser only when a card scrolls into view (geyser.js)."""
    cards = []
    for name, figure in figures_mod.FIGURES.items():
        card = {"name": name, "figure": figure, "grid": None, "poster": None, "legend": None, "error": ""}
        try:
            grid = figures_mod.load(name)
            poster = figures_mod.poster_svg(grid) if grid is not None else None
        except Exception as err:  # noqa: BLE001 - a broken CSV is said on its card; the page never fails over it
            # Our own ValueErrors name the problem; anything else (OSError, csv.Error, ...) only by type, since its
            # message can carry a server path.
            reason = str(err) if type(err) is ValueError else type(err).__name__
            card["error"] = f"{figure.csv} can't be read ({reason})"
            grid = poster = None
        if grid is not None:
            card.update(
                grid=grid,
                poster=mark_safe(poster),  # noqa: S308 - labels escaped in poster_svg
                legend=mark_safe(figures_mod.legend_svg(grid)),  # noqa: S308 - numbers and escaped labels only
            )
        cards.append(card)
    bar_defs = mark_safe(figures_mod.bar_defs()) if any(c["grid"] for c in cards) else ""  # noqa: S308 - constants
    return render(request, "samples/figures.html", {"cards": cards, "bar_defs": bar_defs})


def _grid_or_404(name):
    if name not in figures_mod.FIGURES:
        raise Http404("No such figure")
    try:
        grid = figures_mod.load(name)
    except Exception:  # noqa: BLE001 - unreadable data is "no data", never a 500
        grid = None
    if grid is None:
        raise Http404("No data for this figure yet")
    return grid


@require_GET
@gzip_page
def figure_data(request, name):
    """The grid surface.js draws (axes, rows, values, height cap), as compact JSON."""
    response = HttpResponse(figures_mod.grid_json(_grid_or_404(name)), content_type="application/json")
    response["Cache-Control"] = "public, max-age=300"
    return response


@require_GET
def figure_csv(request, name):
    """The CSV a figure is drawn from: its table view."""
    grid = _grid_or_404(name)
    path = figures_mod.csv_path(grid.figure)
    return FileResponse(open(path, "rb"), as_attachment=True, filename=grid.figure.csv, content_type="text/csv")


@require_POST
def demo(request):
    try:
        sample_id = create_demo_sample(get_storage())
    except DemoError as err:
        messages.error(request, str(err))
        return redirect("samples:list")
    messages.success(request, f"Generated {sample_id}: synthetic reads with known SNPs planted in them.")
    return redirect("samples:detail", sample_id=sample_id)


def _load_sample_or_404(storage, sample_id, static=None):
    # Publishing: from the publisher's listing (no HEAD on the raw reads or alignments, which it may not read).
    sample = static.samples.get(sample_id) if static else status_mod.load_one(storage, sample_id)
    if sample is None:
        raise Http404("No such sample")
    return sample


def _log_tail(storage, sample_id):
    text = read_text(storage, log_key(sample_id))
    if text is None:
        return None
    lines = text.splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _status_context(storage, sample, static=None):
    return {
        "sample": sample,
        **_progress_context(storage, sample),
        "static": static,
        # The static site shows no run log (it can carry paths and commands) and polls the web API, if any.
        "log": None if static else _log_tail(storage, sample.sample_id),
        "status_api": static.status_url(sample.sample_id) if static and sample.active else "",
        "poll_seconds": settings.GEYSER_POLL_SECONDS,
    }


def _progress_context(storage, sample):
    """The one-helix progress bar (samples/_progress.html): where the run is, from this site's earlier stage times."""
    stops = lambda colours: [(round(i / (len(colours) - 1), 4), c) for i, c in enumerate(colours)]  # noqa: E731
    expected = progress_mod.expected_durations(status_mod.read_metrics(storage)) if sample.active else None
    return {
        "progress": progress_mod.progress(sample, expected),
        "helix": progress_mod.HELIX,
        "turbo": stops(figures_mod.HEAT_TURBO),
        "turbo_dark": stops(figures_mod.HEAT_TURBO_DARK),
    }


def _progress_demo_context(request):
    if not settings.DEBUG:
        raise Http404("Debug only")
    try:
        start = float(request.GET.get("start") or "")
    except ValueError:
        start = time.time()
    now = time.time()
    sample, expected = progress_mod.demo_sample(now - start, now)
    titles = {"running": "A run in progress", "succeeded": "Done", "failed": "Failed"}
    stops = lambda colours: [(round(i / (len(colours) - 1), 4), c) for i, c in enumerate(colours)]  # noqa: E731
    return {
        "start": f"{start:.3f}",
        "title": titles.get(sample.state, sample.state),
        "progress": progress_mod.progress(sample, expected, now=now),
        "helix": progress_mod.HELIX,
        "turbo": stops(figures_mod.HEAT_TURBO),
        "turbo_dark": stops(figures_mod.HEAT_TURBO_DARK),
    }


@require_GET
def progress_demo(request):
    """Debug only: the progress helix on a simulated run, looping (a real demo run is over in seconds)."""
    return render(request, "samples/progress_demo.html", _progress_demo_context(request))


@require_GET
def progress_demo_frame(request):
    """The polled fragment of progress_demo (geyser.js replaces it every second, as on a live sample page)."""
    return render(request, "samples/_progress_demo.html", _progress_demo_context(request))


def _current_annotated_vcf(storage, sample):
    """The annotated VCF's key if the sample's last variants run wrote it, else None.

    Nothing deletes an old one when a later variants run doesn't annotate (no GFF, ANNOTATE=off, csq
    failed), so its presence alone could show an earlier run's calls. The variants stage's own part
    (runs/<id>.provenance/CallVariants.json, rewritten by every variants run) lists what that run
    wrote; a later run that stopped before variant calling doesn't touch it. Without a part
    (imported data, runs from before the run record), the file is trusted.
    """
    key = sample.keys["csq_vcf"]
    if not storage.exists(key):
        return None
    try:
        part = read_part(storage, sample.sample_id, "CallVariants")
    except Exception:  # noqa: BLE001 - the part is a hint here; never fail the page over it
        part = None
    if part is None:
        return key
    return key if any(o.get("key") == key for o in part.get("outputs") or []) else None


def _page_number(request, page) -> int:
    """The calls page: from the path (/samples/<id>/calls/<n>/), else the old ?vpage=N; 1 for anything else."""
    if page is None:
        try:
            page = int(request.GET.get("vpage", "1"))
        except ValueError:
            page = 1
    return max(page, 1)


def _calls_url(sample_id: str, page: int) -> str:
    """A calls page as a path, so the same link works on the static site (a folder with an index.html)."""
    if page <= 1:
        return reverse("samples:detail", args=[sample_id]) + "#variants"
    return reverse("samples:detail_page", args=[sample_id, page]) + "#variants"


def _pager(sample_id: str, table, size: int, last_page=None):
    """Previous/next links for the calls (first page inline; each page is its own prerenderable URL).
    last_page: the static site publishes only so many pages; the rest is the VCF download."""
    if table is None or table.total <= size:
        return None
    page = table.offset // size + 1
    has_next = table.last < table.total and (last_page is None or page < last_page)
    return {
        "first": table.first,
        "last": table.last,
        "total": table.total,
        "prev": _calls_url(sample_id, page - 1) if page > 1 else None,
        "next": _calls_url(sample_id, page + 1) if has_next else None,
    }


def variants_key_of(storage, sample) -> str:
    """The VCF the page shows: the csq-annotated copy when this run wrote one, else the run's calls.
    sample.keys["vcf"] is the record's VCF (status.build_sample): DeepVariant's when it was the run's caller,
    and bcftools csq annotates the bcftools VCF only, so the annotated copy applies only to that one."""
    bcftools_calls = sample.keys["vcf"] == keys_for(sample.sample_id)["vcf"]
    annotated = _current_annotated_vcf(storage, sample) if bcftools_calls else None
    return annotated or sample.keys["vcf"]


def _file_href(static, sample_id, kind, key) -> str:
    return static.file_href(sample_id, kind, key) if static else reverse("samples:file", args=[sample_id, kind])


@require_GET
@gzip_page  # HTML only: downloads (CRAM, VCF.gz) are served as they are
def sample_detail(request, sample_id, page=None):
    static = static_of(request)
    storage = get_storage()
    sample = _load_sample_or_404(storage, sample_id, static)
    truth = read_truth(storage, sample_id)
    record = load_record(storage, sample_id)
    bcftools_calls = sample.keys["vcf"] == keys_for(sample_id)["vcf"]
    variants_key = variants_key_of(storage, sample)
    size = max(1, settings.GEYSER_TABLE_ROWS)  # never 0: the page arithmetic divides by it
    offset = (_page_number(request, page) - 1) * size
    preloaded = static.variants.get(variants_key) if static else None
    if preloaded is not None:  # the publisher parsed this VCF once for all its pages
        variants = page_of(preloaded, offset, size)
    else:
        variants = read_variants(storage, variants_key, size, offset, want_calls=truth is not None)
        if variants is not None and variants.total and not variants.rows:  # past the end (stale link): last page
            last = (variants.total - 1) // size * size
            variants = read_variants(storage, variants_key, size, last, want_calls=truth is not None)
    truth_result = check_truth(variants.calls, truth) if (truth and variants) else None
    # Only next to calls: there is nothing to annotate before the variants stage. The published page has no query
    # string, so it shows the default order (highest AVI first).
    alphagenome = (
        alphagenome_mod.read_annotation(storage, sample_id, record, request.GET.get("avi", "desc"), size)
        if variants is not None
        else None
    )
    if alphagenome and alphagenome.state == "not_annotated":
        alphagenome = None  # opt-in per run: no panel at all for a sample that wasn't annotated
    downloads = []
    for kind, label, description in _download_kinds(sample.keys):
        if static and kind not in PUBLIC_KINDS:
            continue  # the public showcase: the calls, QC, gate, stats and run record only (never reads or CRAMs)
        if kind in ALPHAGENOME_KINDS and not (alphagenome and alphagenome.current):
            continue  # not annotated, failed, or an earlier run's files: not this sample's current annotation
        key = _file_key(sample_id, kind, sample.keys)
        if kind in ("csq_vcf", "csq_tbi") and variants_key != sample.keys["csq_vcf"]:
            continue  # an annotated VCF (and its index) from an earlier run: not this sample's current calls
        if kind == "tbi" and not bcftools_calls:
            continue  # the bcftools VCF's index; DeepVariant's VCF is served without one
        if storage.exists(key):
            filename = os.path.basename(key)
            downloads.append(
                {
                    "kind": kind,
                    "label": label,
                    "description": description,
                    "filename": filename,
                    "href": _file_href(static, sample_id, kind, key),
                }
            )
    qc_key = sample.keys["qc_html"]
    qc_report = storage.exists(qc_key)
    if static:
        qc_href = static.file_href(sample_id, "qc_html", qc_key)
    else:
        qc_href = reverse("samples:file", args=[sample_id, "qc_html"]) + "?inline=1"
    shown_calls = static.max_pages * size if static else None
    context = {
        **_status_context(storage, sample, static),
        "truth": truth,
        "truth_result": truth_result,
        "planted": planted_positions(truth),
        "variants": variants,
        "qc_report": qc_report,
        "qc_href": qc_href,
        "qc_summary": read_qc_summary(storage, sample.keys["qc_json"]),
        "downloads": downloads,
        "gate": read_gate(storage, sample.keys["gate_verdict"], record),
        "record": record_view(record),
        "alphagenome": alphagenome,
        "alphagenome_terms_url": alphagenome_mod.TERMS_URL,
        "variant_pager": _pager(sample_id, variants, size, static.max_pages if static else None),
        # The static site publishes the first max_pages pages; past that, the VCF download has the rest.
        "calls_shown": shown_calls if static and variants and variants.total > shown_calls else None,
    }
    return render(request, "samples/detail.html", context)


@require_GET
@gzip_page  # HTML only: downloads (CRAM, VCF.gz) are served as they are
def sample_status(request, sample_id):
    """Polled by geyser.js while a run is active. Once it finishes, the page reloads to show the results."""
    storage = get_storage()
    sample = _load_sample_or_404(storage, sample_id)
    if request.GET.get("was_active") and not sample.active:
        response = HttpResponse(status=204)
        response["Geyser-Refresh"] = "true"
        return response
    return render(request, "samples/_status.html", _status_context(storage, sample))


@require_GET
@xframe_options_sameorigin
def sample_file(request, sample_id, kind):
    split = _split_kind(kind)
    if not is_valid_sample_id(sample_id) or split is None:
        raise Http404("No such file")
    storage = get_storage()
    if split[1] is None and split[0] not in PER_MATE_KEYS:
        keys = keys_for(sample_id)  # bam, vcf, truth, log: one per sample whatever the layout
    else:
        try:
            keys = keys_for(sample_id, layout_from_raw(sample_id, storage.exists))
        except LayoutError:
            raise Http404("No such sample") from None
    if kind in ("vcf", "tbi", "csq_vcf", "csq_tbi"):
        # "vcf" is the sample's current calls (the run record's VCF: DeepVariant's when it was the caller);
        # the bcftools-only files (its index, the annotated copy) don't belong to DeepVariant's calls.
        vcf = sample_vcf_key(load_record(storage, sample_id), sample_id)
        if vcf != keys["vcf"]:
            if kind != "vcf":
                raise Http404("No such file")
            keys = {**keys, "vcf": vcf}
    key = _file_key(sample_id, kind, keys)
    if key is None:
        raise Http404("No such file")  # e.g. raw_R1 for a single-end sample
    try:
        stream = storage.open_read(key)
    except ObjectNotFound:
        raise Http404("No such file") from None
    filename = os.path.basename(key)
    if split[0] == "qc_html" and request.GET.get("inline"):
        # fastp's report is generated HTML: show it in a sandbox (no scripts, opaque origin). Its
        # charts load a script from opengene.org over plain HTTP, so they stay blank here; the
        # tables show. The downloaded file draws them only in a browser that is online.
        response = FileResponse(stream, content_type="text/html; charset=utf-8")
        response["Content-Security-Policy"] = "sandbox"
        return response
    if kind == "log" and request.GET.get("inline"):
        return FileResponse(stream, content_type="text/plain; charset=utf-8")
    return FileResponse(stream, as_attachment=True, filename=filename)
