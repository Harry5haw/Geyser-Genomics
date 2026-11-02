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
- Per-stage breakdown (--stages): Batch queue wait vs run time, vCPU/memory and estimated Fargate cost
  per stage and per run (see stage_breakdown.py); --stages-csv to export it
"""

import argparse
import csv
import json
import os
import shutil
from collections import defaultdict
from datetime import UTC
from statistics import mean, median

import stage_breakdown

DEFAULT_REGION = "eu-west-2"

# ---------- helpers ----------


def list_executions(sfn_client, state_machine_arn, status=None, max_results=50):
    kwargs = {"stateMachineArn": state_machine_arn, "maxResults": max_results}
    if status:
        kwargs["statusFilter"] = status
    return sfn_client.list_executions(**kwargs).get("executions", [])


def describe_execution(sfn_client, execution_arn):
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
        return f"{seconds / 3600:.2f}h"
    elif seconds >= 60:
        return f"{seconds / 60:.2f}m"
    else:
        return f"{seconds:.2f}s"


def fmt_ts(dt):
    """UTC compact timestamp YYYY-MM-DD HH:MM:SS."""
    if not dt:
        return "---"
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def trunc(s: str, width: int) -> str:
    if width <= 3:
        return s[: max(width, 0)]
    return s if len(s) <= width else (s[: width - 3] + "...")


def compute_layout():
    """
    Decide column widths based on terminal width.
    Order: SampleId, Seconds, Runtime, Status, Start, Stop, ExecName
    """
    cols = shutil.get_terminal_size(fallback=(140, 24)).columns

    # base/min widths for each column (keep it readable)
    w = {
        "sample": 16,  # SampleId
        "secs": 11,  # Duration(s)
        "runtime": 8,  # Runtime
        "status": 9,  # SUCCEEDED/FAILED
        "start": 19,  # 2025-09-14 19:15:34
        "stop": 19,
        "exec": 20,  # ExecutionName (gets remaining)
    }
    mins = {"sample": 10, "secs": 9, "runtime": 6, "status": 7, "start": 16, "stop": 16, "exec": 8}

    fixed = w["sample"] + w["secs"] + w["runtime"] + w["status"] + w["start"] + w["stop"]
    spaces = 6  # spaces between 7 columns
    remaining = cols - fixed - spaces

    if remaining >= mins["exec"]:
        w["exec"] = remaining
    else:
        # we’re too wide; shrink some earlier columns down to their mins until it fits
        deficit = mins["exec"] - max(remaining, 0)
        for key in ["start", "stop", "sample", "secs", "status", "runtime"]:
            if deficit <= 0:
                break
            reducible = w[key] - mins[key]
            take = min(reducible, deficit)
            w[key] -= take
            deficit -= take
        w["exec"] = mins["exec"]

    total_line = w["sample"] + w["secs"] + w["runtime"] + w["status"] + w["start"] + w["stop"] + w["exec"] + spaces
    sep_len = min(cols, total_line)
    return w, sep_len


# ---------- main ----------


def fmt_secs(seconds):
    return "---" if seconds is None else smart_format(seconds)


def fmt_cost(usd):
    return "---" if usd is None else f"${usd:.4f}"


STAGE_CSV_HEADER = [
    "SampleId",
    "ExecutionName",
    "Stage",
    "Status",
    "WaitSeconds",
    "RunSeconds",
    "vCPU",
    "MemoryGB",
    "GPUs",
    "EstCostUSD",
    "JobId",
    "Attempts",
    "FailureReason",
    "LogStream",
]


def print_stage_breakdown(sfn_client, batch_client, runs, prices):
    """Print per-stage wait/run/cost for each (sample_id, execution) and return CSV rows."""
    csv_rows, run_costs, waits = [], [], []
    print("\nStage breakdown (wait = queued before the container started; cost = Fargate estimate):")
    for sample_id, exe in runs:
        rows = stage_breakdown.execution_stages(sfn_client, batch_client, exe["executionArn"], prices)
        print(f"\n  {sample_id}  ({exe['name']})")
        print(
            f"    {'Stage':<20} {'Status':<10} {'Wait':>9} {'Run':>9} {'vCPU':>5} {'MemGB':>6} {'Cost':>10} {'Att':>3}"
        )
        for r in rows:
            print(
                f"    {r['stage']:<20} {r['status']:<10} {fmt_secs(r['wait_seconds']):>9} "
                f"{fmt_secs(r['run_seconds']):>9} {r['vcpu'] if r['vcpu'] is not None else '---':>5} "
                f"{'---' if r['memory_gb'] is None else round(r['memory_gb'], 2):>6} "
                f"{('GPU/EC2' if r['gpus'] else fmt_cost(r['cost_usd'])):>10} "
                f"{r['attempts'] if r['attempts'] is not None else '---':>3}"
            )
            if r["reason"]:
                print(f"      reason: {r['reason']}")
            csv_rows.append(
                [
                    sample_id,
                    exe["name"],
                    r["stage"],
                    r["status"],
                    r["wait_seconds"],
                    r["run_seconds"],
                    r["vcpu"],
                    r["memory_gb"],
                    r["gpus"],
                    r["cost_usd"],
                    r["job_id"],
                    r["attempts"],
                    r["reason"],
                    r["log_stream"],
                ]
            )
            if r["wait_seconds"] is not None:
                waits.append(r["wait_seconds"])
        t = stage_breakdown.totals(rows)
        print(
            f"    {'Total':<20} {'':<10} {fmt_secs(t['wait_seconds']):>9} {fmt_secs(t['run_seconds']):>9} "
            f"{'':>5} {'':>6} {fmt_cost(t['cost_usd']):>10}"
        )
        if t["cost_usd"] is not None:
            run_costs.append(t["cost_usd"])
    if run_costs:
        print(
            f"\n  Estimated cost per run: mean {fmt_cost(mean(run_costs))}, max {fmt_cost(max(run_costs))} "
            f"(over {len(run_costs)} runs with full data)"
        )
    if waits:
        print(f"  Queue wait per stage: mean {smart_format(mean(waits))}, max {smart_format(max(waits))}")
    return csv_rows


def main(argv=None, sfn_client=None, batch_client=None):
    parser = argparse.ArgumentParser(description="Query Step Functions pipeline durations.")
    parser.add_argument("--csv", help="Export results to a CSV file", default=None)
    parser.add_argument("--status", help="Filter by status (e.g. SUCCEEDED, FAILED, RUNNING)", default="SUCCEEDED")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    parser.add_argument(
        "--state-machine-arn",
        default=os.environ.get("SFN_ARN"),
        help="Step Functions state machine ARN (default: $SFN_ARN)",
    )
    parser.add_argument("--max-results", type=int, default=50, help="Most recent executions to fetch (default: 50)")
    parser.add_argument(
        "--stages",
        action="store_true",
        help="Also show per-stage queue wait, run time and estimated cost (reads execution history "
        "and Batch describe_jobs)",
    )
    parser.add_argument("--stages-csv", default=None, help="Export the per-stage breakdown to a CSV file")
    parser.add_argument(
        "--vcpu-hour-price",
        type=float,
        default=stage_breakdown.DEFAULT_VCPU_HOUR_USD,
        help=f"Fargate USD per vCPU-hour (default: {stage_breakdown.DEFAULT_VCPU_HOUR_USD}, eu-west-2)",
    )
    parser.add_argument(
        "--gb-hour-price",
        type=float,
        default=stage_breakdown.DEFAULT_GB_HOUR_USD,
        help=f"Fargate USD per GB-hour (default: {stage_breakdown.DEFAULT_GB_HOUR_USD}, eu-west-2)",
    )
    args = parser.parse_args(argv)
    if args.stages_csv:
        args.stages = True

    if not args.state_machine_arn:
        raise SystemExit("FATAL: provide --state-machine-arn or set SFN_ARN")
    if sfn_client is None:
        import boto3

        sfn_client = boto3.client("stepfunctions", region_name=args.region)
    if args.stages and batch_client is None:
        import boto3

        batch_client = boto3.client("batch", region_name=args.region)

    print(f"Fetching recent executions for {args.state_machine_arn} in {args.region} with status={args.status}...\n")

    executions = list_executions(sfn_client, args.state_machine_arn, status=args.status, max_results=args.max_results)
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
    executions_seen = []

    for exe in executions:
        details = describe_execution(sfn_client, exe["executionArn"])
        start = details["startDate"]
        stop = details.get("stopDate")
        status = details["status"]
        duration = (stop - start).total_seconds() if stop else 0.0
        sample_id = extract_sample_id(details)
        runtime_str = smart_format(duration)
        executions_seen.append((sample_id, exe))

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
        rows.append(
            [
                sample_id,
                f"{duration:.2f}",
                runtime_str,
                status,
                fmt_ts(start),
                fmt_ts(stop),
                exe["name"],
            ]
        )

    # Overall summary
    if durations:
        avg_dur = mean(durations)
        min_dur = min(durations)
        max_dur = max(durations)
        med_dur = median(durations)
        print(f"\nOverall Summary ({len(durations)} runs with status={args.status}):")
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

    if args.stages:
        prices = stage_breakdown.Prices(args.vcpu_hour_price, args.gb_hour_price)
        stage_rows = print_stage_breakdown(sfn_client, batch_client, executions_seen, prices)
        if args.stages_csv:
            with open(args.stages_csv, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(STAGE_CSV_HEADER)
                writer.writerows(stage_rows)
            print(f"\nExported {len(stage_rows)} stage rows to {args.stages_csv}")

    # CSV export
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "SampleId",
                    "DurationSeconds",
                    "RuntimeFormatted",
                    "Status",
                    "StartTimeUTC",
                    "StopTimeUTC",
                    "ExecutionName",
                ]
            )
            writer.writerows(rows)
        print(f"\nExported {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
