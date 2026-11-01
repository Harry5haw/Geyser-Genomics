#!/usr/bin/env python3
"""Create or update the Geyser Genomics CloudWatch dashboard.

Widgets: pipeline runs per day and end-to-end time (Step Functions, when a state machine ARN is given),
failures by stage, and stage durations for the core pipeline, DeepVariant and pangenome DeepVariant.
The JSON is built in scripts/monitoring.py.

Preview (no AWS calls):
  python scripts/deploy_dashboard.py --dry-run
  python scripts/deploy_dashboard.py --dry-run --state-machine-arn <arn> --schema aggregated

Deploy (uses your ambient AWS credentials):
  SFN_ARN=<state machine arn> python scripts/deploy_dashboard.py
"""

import argparse
import os
import sys

import monitoring


def deploy(client, dashboard_name: str, body: dict) -> int:
    try:
        resp = client.put_dashboard(DashboardName=dashboard_name, DashboardBody=monitoring.to_json(body))
    except Exception as err:  # noqa: BLE001 - report any AWS error and exit non-zero
        print(f"[ERROR] Failed to put dashboard: {err}", file=sys.stderr)
        return 1
    messages = resp.get("DashboardValidationMessages") or []
    for message in messages:
        print(f"[WARNING] {message}", file=sys.stderr)
    print(f"[OK] Dashboard '{dashboard_name}' deployed{' with warnings' if messages else ''}.")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Deploy the Geyser Genomics CloudWatch dashboard.")
    p.add_argument("--dashboard-name", default="geyser-dashboard-dev")
    p.add_argument("--region", default=monitoring.DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument("--namespace", default=monitoring.DEFAULT_NAMESPACE)
    p.add_argument("--state-machine-arn", default=os.environ.get("SFN_ARN"),
                   help="Step Functions state machine ARN (default: $SFN_ARN). Without it the "
                        "Step Functions widgets are left out.")
    p.add_argument("--schema", choices=monitoring.SCHEMAS, default="per-sample",
                   help="Metric dimensions to read: per-sample (what app/metrics.py publishes today) "
                        "or aggregated (no SampleId; gives real p50/p95)")
    p.add_argument("--groups", default=",".join(monitoring.STAGE_GROUPS),
                   help=f"Comma-separated stage groups (default: all of {','.join(monitoring.STAGE_GROUPS)})")
    p.add_argument("--lookback", default="-P14D", help="Default time range, e.g. -P3D or -P14D")
    p.add_argument("--dry-run", action="store_true", help="Print the dashboard JSON and exit (no AWS calls)")
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    args = parse_args(argv)
    monitoring.check_arn(args.state_machine_arn, "states", "--state-machine-arn")
    try:
        body = monitoring.build_dashboard(
            args.region, args.namespace, state_machine_arn=args.state_machine_arn, schema=args.schema,
            groups=[g.strip() for g in args.groups.split(",") if g.strip()], lookback=args.lookback)
    except ValueError as err:
        raise SystemExit(f"FATAL: {err}")
    if not args.state_machine_arn:
        print("NOTE: no --state-machine-arn/SFN_ARN, so the Step Functions widgets are left out.", file=sys.stderr)

    if args.dry_run:
        print(monitoring.to_json(body))
        return 0
    if client is None:
        import boto3
        client = boto3.client("cloudwatch", region_name=args.region)
    return deploy(client, args.dashboard_name, body)


if __name__ == "__main__":
    sys.exit(main())
