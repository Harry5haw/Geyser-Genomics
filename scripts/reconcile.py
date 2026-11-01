#!/usr/bin/env python3
"""Reconcile uploads, runs and outputs: find what the pipeline silently missed (#66).

Compares the objects in the data lake (S3, or local_bucket/ offline) with what the pipeline expects,
using the pipeline's own key conventions (tasks.keys_for) and the trigger's own sample ID parsing, so
it cannot drift from them.

Upload problems
  ignored        in raw_reads/ but not .gz: the S3 trigger never fires, so nothing happens at all
  will fail      .gz, so a run starts, but decompress reads raw_reads/<id>.fastq.gz and won't find it
                 (e.g. S1.fq.gz, sub/S1.fastq.gz, S1.v2.fastq.gz)

Sample status
  complete       the bcftools VCF exists
  running        an open Step Functions run (needs --state-machine-arn)
  incomplete     some outputs exist but no VCF: failed (or stopped) after the last stage shown
  not started    raw reads only: the trigger didn't fire, or the run failed before writing anything
  orphan         outputs but no raw reads (raw deleted)
With --state-machine-arn, more than one open run for a sample is flagged: they write the same keys.

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
from tasks import keys_for  # noqa: E402

DEFAULT_REGION = "eu-west-2"
_SENTINEL = "SAMPLEIDSENTINEL"
# Pipeline outputs in stage order (keys of tasks.keys_for), with a short column label.
STAGE_OUTPUTS = (("fastq", "D"), ("qc_html", "Q"), ("bam", "A"), ("vcf", "V"))
STAGE_NAMES = {"fastq": "decompress", "qc_html": "qc", "bam": "align", "vcf": "variants"}


def load_trigger():
    """lambda/trigger/index.py, imported by path ('lambda' is a Python keyword)."""
    spec = importlib.util.spec_from_file_location(
        "geyser_trigger_reconcile", os.path.join(REPO_ROOT, "lambda", "trigger", "index.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extra_outputs() -> dict:
    """Optional caller outputs beyond bcftools, keyed by column label. DeepVariant if importable."""
    try:
        from deepvariant_task import deepvariant_keys
    except Exception:  # noqa: BLE001 - the DeepVariant module is optional here
        return {}
    return {"dv": lambda sample_id: deepvariant_keys(sample_id)["vcf"]}


def key_patterns(extras: dict) -> list:
    """Regexes that recover a sample ID from any output key, built from keys_for itself."""
    templates = [v for k, v in keys_for(_SENTINEL).items() if k != "raw"]
    templates += [fn(_SENTINEL) for fn in extras.values()]
    return [re.compile("^" + re.escape(t).replace(_SENTINEL, r"(?P<id>[^/]+)") + "$") for t in templates]


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
    for key in sorted(k for k in keys if k.startswith("raw_reads/")):
        if not key.endswith(".gz"):
            upload_problems.append({"key": key, "problem": "ignored",
                                    "detail": "not .gz, so the S3 trigger never fires and no run starts"})
            continue
        try:
            sample_id = trigger.parse_sample_id(key)
        except ValueError:
            upload_problems.append({"key": key, "problem": "will fail", "detail": "no sample ID in the file name"})
            continue
        expected = keys_for(sample_id)["raw"]
        if key != expected:
            upload_problems.append({"key": key, "problem": "will fail",
                                    "detail": f"starts a run for '{sample_id}', but decompress reads {expected}"})
            continue
        samples.add(sample_id)

    for pattern in key_patterns(extras):
        for key in keys:
            match = pattern.match(key)
            if match:
                samples.add(match.group("id"))
    samples |= {s for s in running if s != "Unknown"}

    rows = []
    for sample_id in sorted(samples):
        expected = keys_for(sample_id)
        outputs = {name: expected[name] in keys for name, _ in STAGE_OUTPUTS}
        extra = {label: fn(sample_id) in keys for label, fn in extras.items()}
        has_raw = expected["raw"] in keys
        reached = [STAGE_NAMES[name] for name, _ in STAGE_OUTPUTS if outputs[name]]
        open_count = running.get(sample_id, 0)
        problem = None
        if not has_raw and (reached or any(extra.values())):
            status, note = "orphan", "outputs exist but the raw reads are gone"
        elif open_count:
            status, note = "running", f"{open_count} open run(s)"
        elif outputs["vcf"]:
            status, note = "complete", ""
        elif reached:
            status, note = "incomplete", f"no VCF; last output from '{reached[-1]}'"
            problem = "incomplete"
        else:
            status, note = "not started", "raw reads only: no run, or it failed before writing any output"
            problem = "not started"
        if open_count > 1:
            note = f"{open_count} runs at once: they overwrite each other's outputs"
            problem = "concurrent runs"
        rows.append({"sample_id": sample_id, "status": status, "raw": has_raw, "outputs": outputs,
                     "extra_outputs": extra, "open_runs": open_count, "note": note, "problem": problem})

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
        print(f"{flag}{r['sample_id']:<23} {r['status']:<12} {marks:<{2 * len(labels) + 3 * len(extra_labels)}} "
              f"{r['note']}")
    print("Outputs: D decompressed, Q QC report, A alignment, V VCF" + (", dv DeepVariant VCF" if extra_labels else ""))
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

    result = reconcile(storage.list_keys(""), running)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)
    return 1 if args.fail_on_problems and result["problem_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
