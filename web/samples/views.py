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

from geyser_web.pipeline import ObjectNotFound, get_storage, keys_for

from . import status as status_mod
from .demo import DemoError, create_demo_sample
from .forms import UploadForm
from .runs import is_valid_sample_id, log_key, read_text
from .vcf import check_truth, planted_positions, read_truth, read_variants, truth_key

# Downloadable files: kind -> (label, description). Anything not listed here is a 404.
FILE_KINDS = {
    "raw": ("Raw reads", "Gzipped FASTQ as uploaded"),
    "fastq": ("Decompressed reads", "Plain FASTQ"),
    "qc_html": ("FastQC report", "HTML"),
    "qc_zip": ("FastQC data", "ZIP"),
    "bam": ("Alignments", "Coordinate-sorted BAM"),
    "vcf": ("Variants", "Gzipped VCF"),
    "truth": ("Planted SNPs", "Truth file for demo samples (JSON)"),
    "log": ("Run log", "Output of the local pipeline run"),
}
LOG_TAIL_LINES = 300


def _file_key(sample_id, kind):
    if kind == "truth":
        return truth_key(sample_id)
    if kind == "log":
        return log_key(sample_id)
    return keys_for(sample_id).get(kind)


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
    if declared > UploadForm.max_bytes() + 1024 * 1024:
        messages.error(request, f"That file is larger than the {settings.GEYSER_MAX_UPLOAD_MB} MB limit.")
        return redirect("samples:list")

    form = UploadForm(request.POST, request.FILES, storage=storage)
    if not form.is_valid():
        return render(request, "samples/list.html", _list_context(storage, form), status=400)
    sample_id = form.cleaned_data["sample_id"]
    reads = form.cleaned_data["reads"]
    reads.seek(0)
    storage.upload_stream(reads, keys_for(sample_id)["raw"])
    messages.success(request, f"Uploaded {sample_id}. The pipeline starts as soon as the trigger sees it.")
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


@require_GET
def sample_detail(request, sample_id):
    storage = get_storage()
    sample = _load_sample_or_404(storage, sample_id)
    truth = read_truth(storage, sample_id)
    variants = read_variants(storage, sample.keys["vcf"], settings.GEYSER_VARIANT_ROWS)
    truth_result = check_truth(variants.calls, truth) if (truth and variants) else None
    downloads = []
    for kind, (label, description) in FILE_KINDS.items():
        key = _file_key(sample_id, kind)
        if storage.exists(key):
            downloads.append({"kind": kind, "label": label, "description": description,
                              "filename": os.path.basename(key)})
    context = {
        **_status_context(storage, sample),
        "truth": truth,
        "truth_result": truth_result,
        "planted": planted_positions(truth),
        "variants": variants,
        "has_qc": storage.exists(sample.keys["qc_html"]),
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
    if not is_valid_sample_id(sample_id) or kind not in FILE_KINDS:
        raise Http404("No such file")
    key = _file_key(sample_id, kind)
    try:
        stream = get_storage().open_read(key)
    except ObjectNotFound:
        raise Http404("No such file") from None
    filename = os.path.basename(key)
    if kind == "qc_html" and request.GET.get("inline"):
        # FastQC's report is generated HTML: show it in a sandbox (no scripts, opaque origin).
        response = FileResponse(stream, content_type="text/html; charset=utf-8")
        response["Content-Security-Policy"] = "sandbox"
        return response
    if kind == "log" and request.GET.get("inline"):
        return FileResponse(stream, content_type="text/plain; charset=utf-8")
    return FileResponse(stream, as_attachment=True, filename=filename)
