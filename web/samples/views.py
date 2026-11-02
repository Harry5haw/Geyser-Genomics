"""Views. Every one of them only reads or writes pipeline storage keys; none starts a run.

Uploading to raw_reads/ is the trigger, exactly as in the cloud (S3 event -> Lambda). Locally
the worker (manage.py runworker) notices the new key; in the cloud the Lambda does.
"""

import os

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from geyser_web.pipeline import (
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
)

from . import status as status_mod
from .demo import DemoError, create_demo_sample
from .forms import UploadForm
from .qc import read_qc_summary
from .runs import is_valid_sample_id, log_key, read_text
from .vcf import check_truth, planted_positions, read_truth, read_variants, truth_key

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
    "flagstat": ("Alignment stats", "samtools flagstat: reads, mapped, duplicates, properly paired"),
    "mosdepth_summary": ("Coverage summary", "mosdepth: mean, min and max depth per contig (duplicates excluded)"),
    "mosdepth_dist": ("Coverage distribution", "mosdepth: fraction of bases covered at each depth or more"),
    "bcftools_stats": ("Variant stats", "bcftools stats: SNP and indel counts, Ti/Tv, quality distributions"),
    "truth": ("Planted SNPs", "Truth file for demo samples (JSON)"),
    "log": ("Run log", "Output of the local pipeline run"),
}
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
    return keys.get(kind)


def _download_kinds(keys):
    """(kind, label, description) for every file a sample can have, both mates for paired-end."""
    for kind, (label, description) in FILE_KINDS.items():
        if kind in keys or kind in ("truth", "log"):
            yield kind, label, description
        else:
            for mate in MATES:
                yield f"{kind}_{mate}", f"{label} ({mate})", description


def _list_context(storage, form):
    samples = status_mod.load_all(storage)
    return {
        "form": form,
        "samples": samples,
        "any_active": any(s.active for s in samples),
        "poll_seconds": settings.GEYSER_POLL_SECONDS,
        "stages": status_mod.STAGES,
        "max_upload_mb": settings.GEYSER_MAX_UPLOAD_MB,
        "max_upload_bytes": UploadForm.max_bytes(),
    }


@require_GET
def sample_list(request):
    storage = get_storage()
    return render(request, "samples/list.html", _list_context(storage, UploadForm(storage=storage)))


@require_GET
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
        messages.error(request, f"That file is larger than the {settings.GEYSER_MAX_UPLOAD_MB} MB limit.")
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


@require_POST
def demo(request):
    try:
        sample_id = create_demo_sample(get_storage())
    except DemoError as err:
        messages.error(request, str(err))
        return redirect("samples:list")
    messages.success(request, f"Generated {sample_id}: synthetic reads with known SNPs planted in them.")
    return redirect("samples:detail", sample_id=sample_id)


def _load_sample_or_404(storage, sample_id):
    sample = status_mod.load_one(storage, sample_id)
    if sample is None:
        raise Http404("No such sample")
    return sample


def _log_tail(storage, sample_id):
    text = read_text(storage, log_key(sample_id))
    if text is None:
        return None
    lines = text.splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _status_context(storage, sample):
    return {
        "sample": sample,
        "log": _log_tail(storage, sample.sample_id),
        "poll_seconds": settings.GEYSER_POLL_SECONDS,
    }


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


@require_GET
def sample_detail(request, sample_id):
    storage = get_storage()
    sample = _load_sample_or_404(storage, sample_id)
    truth = read_truth(storage, sample_id)
    # The annotated VCF has the same records plus consequences: show it when the latest run wrote it.
    variants_key = _current_annotated_vcf(storage, sample) or sample.keys["vcf"]
    variants = read_variants(storage, variants_key, settings.GEYSER_VARIANT_ROWS)
    truth_result = check_truth(variants.calls, truth) if (truth and variants) else None
    downloads = []
    for kind, label, description in _download_kinds(sample.keys):
        key = _file_key(sample_id, kind, sample.keys)
        if kind == "csq_vcf" and variants_key != key:
            continue  # an annotated VCF from an earlier run: not this sample's current calls
        if storage.exists(key):
            downloads.append(
                {"kind": kind, "label": label, "description": description, "filename": os.path.basename(key)}
            )
    context = {
        **_status_context(storage, sample),
        "truth": truth,
        "truth_result": truth_result,
        "planted": planted_positions(truth),
        "variants": variants,
        "qc_report": storage.exists(sample.keys["qc_html"]),
        "qc_summary": read_qc_summary(storage, sample.keys["qc_json"]),
        "downloads": downloads,
    }
    return render(request, "samples/detail.html", context)


@require_GET
def sample_status(request, sample_id):
    """Polled by htmx while a run is active. Once it finishes, reload the page to show the results."""
    storage = get_storage()
    sample = _load_sample_or_404(storage, sample_id)
    if request.GET.get("was_active") and not sample.active:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
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
