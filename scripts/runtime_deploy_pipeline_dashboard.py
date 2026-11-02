#!/usr/bin/env python3
"""Create or update a small dashboard with only the whole-pipeline Step Functions widgets
(runs per day and end-to-end time from AWS/States ExecutionTime).

The full dashboard (scripts/deploy_dashboard.py) already includes these widgets when given the state
machine ARN; use this one if you want them on their own.

Usage:
  python scripts/runtime_deploy_pipeline_dashboard.py --state-machine-arn <arn> --dry-run
  SFN_ARN=<arn> python scripts/runtime_deploy_pipeline_dashboard.py
"""

import argparse
import os
import sys

import monitoring
from deploy_dashboard import deploy


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Deploy the pipeline runtime dashboard (Step Functions metrics).")
    p.add_argument("--dashboard-name", default=os.environ.get("PIPELINE_DASHBOARD_NAME", "geyser-pipeline-runtime"))
    p.add_argument("--region", default=monitoring.DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument(
        "--state-machine-arn",
        default=os.environ.get("SFN_ARN"),
        help="Step Functions state machine ARN (default: $SFN_ARN)",
    )
    p.add_argument("--lookback", default="-P14D", help="Default time range, e.g. -P3D or -P14D")
    p.add_argument("--dry-run", action="store_true", help="Print the dashboard JSON and exit (no AWS calls)")
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    args = parse_args(argv)
    if not args.state_machine_arn:
        raise SystemExit("FATAL: provide --state-machine-arn or set SFN_ARN")
    monitoring.check_arn(args.state_machine_arn, "states", "--state-machine-arn")
    body = monitoring.build_pipeline_runtime_dashboard(args.region, args.state_machine_arn, args.lookback)

    if args.dry_run:
        print(monitoring.to_json(body))
        return 0
    if client is None:
        import boto3

        client = boto3.client("cloudwatch", region_name=args.region)
    return deploy(client, args.dashboard_name, body)


if __name__ == "__main__":
    sys.exit(main())
