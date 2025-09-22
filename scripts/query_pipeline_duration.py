#!/usr/bin/env python3
"""
Query Step Functions executions and calculate total pipeline duration.

- Columns (left→right): SampleId, Duration(s), Runtime, Status, StartTime, StopTime, ExecutionName
- Auto-detects terminal width and compacts to avoid wrapping
- Extracts SampleId from execution input (`srr_id`)
- Smart unit formatting for Runtime (s/m/h)
- Prints per-execution rows + overall and per-sample summaries
- Optional CSV export (--csv filename)
- Status filter (--status SUCCEEDED|FAILED|RUNNING|...)
"""

import os
import json
import csv
import argparse
import shutil
from collections import defaultdict
from statistics import mean, median
from datetime import timezone

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
STATE_MACHINE_ARN = os.environ.get("SFN_ARN")  # must be set in your env

if not STATE_MACHINE_ARN:
    print("FATAL: SFN_ARN environment variable is not set.")
    exit(1)

sfn_client = boto3.client("stepfunctions", region_name=AWS_REGION)

# ---------- helpers ----------

def list_executions(state_machine_arn, status=None, max_results=50):
    kwargs = {"stateMachineArn": state_machine_arn, "maxResults": max_results}
    if status:
        kwargs["statusFilter"] = status
    return sfn_client.list_executions(**kwargs).get("executions", [])

def describe_execution(execution_arn):
    return sfn_client.describe_execution(executionArn=execution_arn)

def extract_sample_id(execution_details: dict) -> str:
    """Extract SampleId (srr_id) from execution input JSON."""
    try:
        payload = execution_details.get("input", "{}")
        data = json.loads(payload) if isinstance(payload, str) else payload
        return str(data.get("srr_id", "Unknown"))
    except Exception:
        return "Unknown"

def smart_format(seconds: float) -> str:
    """Scale-aware runtime string (s/m/h)."""
    if seconds >= 3600:
        return f"{seconds/3600:.2f}h"
    elif seconds >= 60:
        return f"{seconds/60:.2f}m"
    else:
        return f"{seconds:.2f}s"

def fmt_ts(dt):
    """UTC compact timestamp YYYY-MM-DD HH:MM:SS."""
    if not dt:
        return '---'
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

def trunc(s: str, width: int) -> str:
    if width <= 3:
        return s[:max(width,0)]
    return s if len(s) <= width else (s[:width-3] + '...')

def compute_layout():
    """
    Decide column widths based on terminal width.
    Order: SampleId, Seconds, Runtime, Status, Start, Stop, ExecName
    """
    cols = shutil.get_terminal_size(fallback=(140, 24)).columns

    # base/min widths for each column (keep it readable)
    w = {
        "sample": 16,   # SampleId
        "secs":   11,   # Duration(s)
        "runtime": 8,   # Runtime
        "status":  9,   # SUCCEEDED/FAILED
        "start":  19,   # 2025-09-14 19:15:34
        "stop":   19,
        "exec":   20,   # ExecutionName (gets remaining)
    }
    mins = {
        "sample": 10, "secs": 9, "runtime": 6, "status": 7, "start": 16, "stop": 16, "exec": 8
    }

    fixed = w["sample"] + w["secs"] + w["runtime"] + w["status"] + w["start"] + w["stop"]
    spaces = 6  # spaces between 7 columns
    remaining = cols - fixed - spaces

    if remaining >= mins["exec"]:
        w["exec"] = remaining
    else:
        # we’re too wide; shrink some earlier columns down to their mins until it fits
        deficit = mins["exec"] - max(remaining, 0)
        for key in ["start", "stop", "sample", "secs", "status", "runtime"]:
            if deficit <= 0: break
            reducible = w[key] - mins[key]
            take = min(reducible, deficit)
            w[key] -= take
            deficit -= take
        w["exec"] = mins["exec"]

    total_line = w["sample"] + w["secs"] + w["runtime"] + w["status"] + w["start"] + w["stop"] + w["exec"] + spaces
    sep_len = min(cols, total_line)
    return w, sep_len

# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Query Step Functions pipeline durations.")
    parser.add_argument("--csv", help="Export results to a CSV file", default=None)
    parser.add_argument("--status", help="Filter by status (e.g. SUCCEEDED, FAILED, RUNNING)", default="SUCCEEDED")
    args = parser.parse_args()

    print(f"Fetching recent executions for {STATE_MACHINE_ARN} in {AWS_REGION} with status={args.status}...\n")

    executions = list_executions(STATE_MACHINE_ARN, status=args.status, max_results=50)
    if not executions:
        print("No executions found.")
        return

    widths, sep_len = compute_layout()
    # Header (requested order)
    print(
        f"{'SampleId':<{widths['sample']}} "
        f"{'Duration(s)':<{widths['secs']}} "
        f"{'Runtime':<{widths['runtime']}} "
        f"{'Status':<{widths['status']}} "
        f"{'StartTime':<{widths['start']}} "
        f"{'StopTime':<{widths['stop']}} "
        f"{'ExecutionName':<{widths['exec']}}"
    )
    print("-" * sep_len)

    durations = []
    sample_durations = defaultdict(list)
    rows = []

    for exe in executions:
        details = describe_execution(exe["executionArn"])
        start = details["startDate"]
        stop = details.get("stopDate")
        status = details["status"]
        duration = (stop - start).total_seconds() if stop else 0.0
        sample_id = extract_sample_id(details)
        runtime_str = smart_format(duration)

        # Collect for summaries
        if status in ("SUCCEEDED", "FAILED"):
            durations.append(duration)
            sample_durations[sample_id].append(duration)

        # Row (respect widths + truncation for name)
        print(
            f"{trunc(sample_id, widths['sample']):<{widths['sample']}} "
            f"{duration:<{widths['secs']}.2f} "
            f"{runtime_str:<{widths['runtime']}} "
            f"{status:<{widths['status']}} "
            f"{fmt_ts(start):<{widths['start']}} "
            f"{fmt_ts(stop):<{widths['stop']}} "
            f"{trunc(exe['name'], widths['exec']):<{widths['exec']}}"
        )

        # CSV row in the same order
        rows.append([
            sample_id,
            f"{duration:.2f}",
            runtime_str,
            status,
            fmt_ts(start),
            fmt_ts(stop),
            exe["name"],
        ])

    # Overall summary
    if durations:
        avg_dur = mean(durations)
        min_dur = min(durations)
        max_dur = max(durations)
        med_dur = median(durations)
        print("\nOverall Summary ({} runs with status={}):".format(len(durations), args.status))
        print(f"  Average: {smart_format(avg_dur)} ({avg_dur:.2f}s)")
        print(f"  Median:  {smart_format(med_dur)} ({med_dur:.2f}s)")
        print(f"  Min:     {smart_format(min_dur)} ({min_dur:.2f}s)")
        print(f"  Max:     {smart_format(max_dur)} ({max_dur:.2f}s)")

    # Per-sample summary
    if sample_durations:
        print("\nPer-Sample Average Durations:")
        for sample, runs in sorted(sample_durations.items()):
            avg = mean(runs)
            print(f"  {sample:<20} {smart_format(avg)} (avg over {len(runs)} runs)")

    # CSV export
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["SampleId", "DurationSeconds", "RuntimeFormatted", "Status", "StartTimeUTC", "StopTimeUTC", "ExecutionName"])
            writer.writerows(rows)
        print(f"\nExported {len(rows)} rows to {args.csv}")

if __name__ == "__main__":
    main()
