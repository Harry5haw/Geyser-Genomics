#!/usr/bin/env python3
"""Reconcile uploads, runs and outputs: find what the pipeline silently missed (#66).

Compares the objects in the data lake (S3, or local_bucket/ offline) with what the pipeline expects,
using the pipeline's own key conventions (tasks.keys_for) and the trigger's own sample ID parsing, so
it cannot drift from them.

Single-end samples are raw_reads/<id>.fastq.gz; paired-end samples are raw_reads/<id>_R1.fastq.gz plus
raw_reads/<id>_R2.fastq.gz; every output after them is one per sample (one fastp report for both mates).

Upload problems
  ignored        in raw_reads/ but not .gz: the S3 trigger never fires, so nothing happens at all
  refused        a mate not named exactly <id>_R1.fastq.gz / <id>_R2.fastq.gz (e.g. S1_R1.fq.gz): the
                 trigger raises an error (trigger Lambda Errors) and no run starts
  will fail      .gz, so a run starts, but the stages read raw_reads/<id>.fastq.gz (or <id>_R1/_R2) and
                 won't find it (e.g. S1.fq.gz, sub/S1.fastq.gz, S1.v2.fastq.gz)

Sample status
  lone mate      one paired-end mate uploaded, the other missing: the trigger waits, no run starts
  both layouts   <id>.fastq.gz and <id>_R1/_R2 both uploaded: every stage fails rather than guess
  complete       the bcftools VCF exists
  running        an open Step Functions run (needs --state-machine-arn)
  incomplete     some outputs exist but no VCF: a stage failed or stopped (the note says which wrote output)
  not started    raw reads only: the trigger didn't fire, or the run failed before writing anything
  orphan         outputs but no raw reads (raw deleted)
With --state-machine-arn, more than one open run for a sample is flagged: they write the same keys.
Without it, open runs are not checked, so a sample that is still running shows as not started or
incomplete: run with the state machine ARN before trusting --fail-on-problems.

Usage:
  python scripts/reconcile.py --local-dir local_bucket
  python scripts/reconcile.py --bucket <data lake bucket> --state-machine-arn <arn>
  python scripts/reconcile.py ... --json --fail-on-problems
"""

import argparse
import importlib.util
import json
import os
import re
import sys
from collections import Counter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("app", "app-deepvariant"):
    _path = os.path.join(REPO_ROOT, _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from storage import LocalStorage, S3Storage, storage_from_env  # noqa: E402
from tasks import MATES, PAIRED, PER_MATE_KEYS, SINGLE, keys_for, keys_of  # noqa: E402

DEFAULT_REGION = "eu-west-2"
_SENTINEL = "SAMPLEIDSENTINEL"
# Pipeline outputs in stage order (keys of tasks.keys_for), with a short column label.
# qc and align run side by side, so a sample can have A without Q (or the other way round).
STAGE_OUTPUTS = (("qc_html", "Q"), ("bam", "A"), ("vcf", "V"))
STAGE_NAMES = {"qc_html": "qc", "bam": "align", "vcf": "variants"}


def load_trigger():
    """lambda/trigger/index.py, imported by path ('lambda' is a Python keyword)."""
    spec = importlib.util.spec_from_file_location(
        "geyser_trigger_reconcile", os.path.join(REPO_ROOT, "lambda", "trigger", "index.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extra_outputs() -> dict:
    """Optional caller outputs beyond bcftools, keyed by column label: DeepVariant (dv) and
    pangenome-aware DeepVariant (pg), if the DeepVariant module is importable."""
    try:
        from deepvariant_task import deepvariant_keys
    except Exception:  # noqa: BLE001 - the DeepVariant module is optional here
        return {}
    # The pangenome VCF key doesn't depend on which graph was used; any name gives the same key.
    return {"dv": lambda sample_id: deepvariant_keys(sample_id)["vcf"],
            "pg": lambda sample_id: deepvariant_keys(sample_id, pangenome="graph.gbz")["vcf"]}


def key_patterns(extras: dict) -> list:
    """Regexes that recover a sample ID from any output key, built from keys_for itself.

    Paired-end patterns come first, so raw-derived per-mate keys belong to sample S1, not a single-end
    sample S1_R1 (which can't exist: the trigger reads a trailing _R1/_R2 as a mate).
    """
    templates = []
    for layout in (PAIRED, SINGLE):
        templates += [v for k, v in keys_for(_SENTINEL, layout).items()
                      if not k.startswith("raw") and v not in templates]
    templates += [fn(_SENTINEL) for fn in extras.values()]
    return [re.compile("^" + re.escape(t).replace(_SENTINEL, r"(?P<id>[^/]+)") + "$") for t in templates]


def match_sample(key: str, patterns) -> str:
    """The sample ID an output key belongs to (first matching pattern), or None."""
    for pattern in patterns:
        match = pattern.match(key)
        if match:
            return match.group("id")
    return None


def raw_upload_problem(trigger, key: str):
    """(sample_id, None) for a raw key the stages will read, else (None, problem)."""
    if not key.endswith(".gz"):
        return None, {"key": key, "problem": "ignored",
                      "detail": "not .gz, so the S3 trigger never fires and no run starts"}
    try:
        sample_id, mate = trigger.parse_raw_key(key)
    except ValueError:
        return None, {"key": key, "problem": "will fail", "detail": "no sample ID in the file name"}
    if mate is None:
        expected = keys_for(sample_id, SINGLE)["raw"]
    else:
        try:
            partner = trigger.mate_key(key)
        except ValueError as err:
            return None, {"key": key, "problem": "refused", "detail": f"the trigger raises: {err}"}
        expected = keys_for(sample_id, PAIRED)[f"raw_{mate}"]
        if key != expected:
            # The trigger keeps the folder when it looks for the partner, but the stages don't.
            return None, {"key": key, "problem": "will fail",
                          "detail": f"the trigger waits for {partner}, then starts a run for '{sample_id}', "
                                    f"but the stages read {expected}"}
    if key != expected:
        return None, {"key": key, "problem": "will fail",
                      "detail": f"starts a run for '{sample_id}', but the stages read {expected}"}
    return sample_id, None


def raw_layout(sample_id: str, keys: set):
    """(layout, problem, note) from which raw reads exist. layout is None if they don't form one sample."""
    single = keys_for(sample_id, SINGLE)["raw"] in keys
    mates = [m for m in MATES if keys_for(sample_id, PAIRED)[f"raw_{m}"] in keys]
    if single and mates:
        return None, "both layouts", (f"{sample_id}.fastq.gz and _{'/_'.join(mates)} both uploaded: "
                                      "every stage fails rather than guess")
    if len(mates) == 1:
        missing = next(m for m in MATES if m not in mates)
        return None, "lone mate", f"{mates[0]} uploaded, {missing} missing: the trigger waits, no run starts"
    if single:
        return SINGLE, None, ""
    if mates:
        return PAIRED, None, ""
    return None, None, ""


def output_layout(sample_id: str, keys: set) -> str:
    """The layout a sample's outputs were written in, for when its raw reads don't say."""
    paired = keys_for(sample_id, PAIRED)
    per_mate = [k for name in PER_MATE_KEYS if name != "raw" for k in keys_of(paired, name)]
    return PAIRED if any(k in keys for k in per_mate) else SINGLE


def open_runs(sfn_client, state_machine_arn: str) -> Counter:
    """Number of RUNNING executions per sample ID (from each execution's input)."""
    counts = Counter()
    kwargs = {"stateMachineArn": state_machine_arn, "statusFilter": "RUNNING", "maxResults": 1000}
    while True:
        page = sfn_client.list_executions(**kwargs)
        for execution in page.get("executions", []):
            details = sfn_client.describe_execution(executionArn=execution["executionArn"])
            try:
                counts[str(json.loads(details.get("input") or "{}").get("srr_id", "Unknown"))] += 1
            except ValueError:
                counts["Unknown"] += 1
        if not page.get("nextToken"):
            return counts
        kwargs["nextToken"] = page["nextToken"]


def reconcile(keys, running: Counter = None) -> dict:
    keys = set(keys)
    running = running or Counter()
    trigger = load_trigger()
    extras = extra_outputs()

    upload_problems, samples = [], set()
    # Keys ending in "/" are folder markers (the S3 console's "Create folder"), not uploads.
    for key in sorted(k for k in keys if k.startswith("raw_reads/") and not k.endswith("/")):
        sample_id, problem = raw_upload_problem(trigger, key)
        if problem:
            upload_problems.append(problem)
        else:
            samples.add(sample_id)

    patterns = key_patterns(extras)
    samples |= {s for s in (match_sample(k, patterns) for k in keys) if s}
    samples |= {s for s in running if s != "Unknown"}

    rows = []
    for sample_id in sorted(samples):
        layout, layout_problem, layout_note = raw_layout(sample_id, keys)
        has_raw = layout is not None or layout_problem is not None
        expected = keys_for(sample_id, layout or output_layout(sample_id, keys))
        # A per-mate output (the QC reports) only counts once both mates have it.
        outputs = {name: all(k in keys for k in keys_of(expected, name)) for name, _ in STAGE_OUTPUTS}
        extra = {label: fn(sample_id) in keys for label, fn in extras.items()}
        # But one mate's output still shows the run got that far.
        reached = [STAGE_NAMES[name] for name, _ in STAGE_OUTPUTS
                   if any(k in keys for k in keys_of(expected, name))]
        open_count = running.get(sample_id, 0)
        problem = None
        if layout_problem:
            status, note, problem = layout_problem, layout_note, layout_problem
        elif not has_raw and (reached or any(extra.values())):
            status, note = "orphan", "outputs exist but the raw reads are gone"
        elif open_count:
            status, note = "running", f"{open_count} open run(s)"
        elif outputs["vcf"]:
            status, note = "complete", ""
        elif reached:
            missing = [STAGE_NAMES[n] for n, _ in STAGE_OUTPUTS if STAGE_NAMES[n] not in reached]
            status, note = "incomplete", f"no VCF; output from {', '.join(reached)}; none from {', '.join(missing)}"
            problem = "incomplete"
        else:
            status, note = "not started", "raw reads only: no run, or it failed before writing any output"
            problem = "not started"
        if open_count > 1:
            note = f"{open_count} runs at once: they overwrite each other's outputs"
            problem = "concurrent runs"
        rows.append({"sample_id": sample_id, "status": status, "layout": layout, "raw": has_raw,
                     "outputs": outputs, "extra_outputs": extra, "open_runs": open_count, "note": note,
                     "problem": problem})

    return {"samples": rows, "upload_problems": upload_problems,
            "problem_count": len(upload_problems) + sum(1 for r in rows if r["problem"])}


def print_report(result: dict) -> None:
    labels = [label for _, label in STAGE_OUTPUTS]
    extra_labels = sorted({label for r in result["samples"] for label in r["extra_outputs"]})
    print(f"{'Sample':<24} {'Status':<12} {'Outputs':<{2 * len(labels) + 3 * len(extra_labels)}} Note")
    for r in result["samples"]:
        marks = " ".join(label if r["outputs"][name] else "." for name, label in STAGE_OUTPUTS)
        marks += "".join(f" {label if r['extra_outputs'].get(label) else '.' * len(label)}" for label in extra_labels)
        flag = "!" if r["problem"] else " "
        note = "; ".join(n for n in ("paired-end" if r["layout"] == PAIRED else "", r["note"]) if n)
        print(f"{flag}{r['sample_id']:<23} {r['status']:<12} {marks:<{2 * len(labels) + 3 * len(extra_labels)}} "
              f"{note}")
    print("Outputs: Q QC report (fastp), A alignment, V VCF"
          + (", dv DeepVariant VCF, pg pangenome DeepVariant VCF" if extra_labels else ""))
    if result["upload_problems"]:
        print("\nUpload problems:")
        for p in result["upload_problems"]:
            print(f"! {p['key']:<40} {p['problem']:<10} {p['detail']}")
    counts = Counter(r["status"] for r in result["samples"])
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "no samples"
    print(f"\n{summary}; {result['problem_count']} problem(s)")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Reconcile uploads, runs and outputs in the data lake.")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--local-dir", help="Local storage root (e.g. local_bucket)")
    where.add_argument("--bucket", help="S3 data lake bucket (default: from STORAGE_BACKEND/BUCKET_NAME)")
    p.add_argument("--region", default=DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument("--state-machine-arn", default=os.environ.get("SFN_ARN"),
                   help="Also check open Step Functions runs (default: $SFN_ARN; skipped if unset)")
    p.add_argument("--json", action="store_true", help="Print the result as JSON")
    p.add_argument("--fail-on-problems", action="store_true", help="Exit 1 if any problem is found")
    return p.parse_args(argv)


def main(argv=None, storage=None, sfn_client=None) -> int:
    args = parse_args(argv)
    if storage is None:
        if args.local_dir:
            storage = LocalStorage(args.local_dir)
        elif args.bucket:
            storage = S3Storage(args.bucket, args.region)
        else:
            storage = storage_from_env()
    running = Counter()
    if args.state_machine_arn:
        if sfn_client is None:
            import boto3
            sfn_client = boto3.client("stepfunctions", region_name=args.region)
        running = open_runs(sfn_client, args.state_machine_arn)
    else:
        print("NOTE: no --state-machine-arn/SFN_ARN, so open runs aren't checked: a sample still being "
              "processed shows as 'not started' or 'incomplete'.", file=sys.stderr)

    result = reconcile(storage.list_keys(""), running)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)
    return 1 if args.fail_on_problems and result["problem_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
