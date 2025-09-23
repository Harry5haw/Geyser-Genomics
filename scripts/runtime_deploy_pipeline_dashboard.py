#!/usr/bin/env python3
"""
Create/update a CloudWatch dashboard that shows whole-pipeline runtime
using the built-in Step Functions metric AWS/States:ExecutionTime (ms),
converted to seconds via metric math.

Defaults:
- Region:        $AWS_REGION or eu-west-2
- State Machine: $SFN_ARN (must be set)
- Lookback:      -P14D (last 14 days)

Usage:
  python scripts/runtime_deploy_pipeline_dashboard.py --dry-run | jq .
  python scripts/runtime_deploy_pipeline_dashboard.py
  python scripts/runtime_deploy_pipeline_dashboard.py --dashboard-name geyser-dashboard-dev
"""

import os
import json
import argparse
import boto3


def build_dashboard_body(region: str, state_machine_arn: str, lookback: str = "-P14D"):
    """
    Single, robust widget based on AWS/States ExecutionTime (ms) -> seconds.
    """
    return {
        "start": lookback,
        "periodOverride": "inherit",
        "widgets": [
            {
                "type": "metric",
                "x": 0, "y": 0, "width": 24, "height": 8,
                "properties": {
                    "region": region,
                    "view": "timeSeries",
                    "stacked": False,
                    "title": "Pipeline Runtime (seconds) — from AWS/States ExecutionTime",
                    "period": 300,
                    "yAxis": {"left": {"min": 0, "label": "Seconds"}},
                    # m1 = ExecutionTime (ms) averaged per 5-min bucket
                    # e1 = m1/1000 -> seconds
                    "metrics": [
                        ["AWS/States", "ExecutionTime", "StateMachineArn", state_machine_arn, {"id": "m1", "stat": "Average", "visible": False}],
                        [{"expression": "m1/1000", "label": "ExecutionTime (s)", "id": "e1"}]
                    ]
                }
            }
        ]
    }


def main():
    parser = argparse.ArgumentParser(description="Deploy CloudWatch dashboard for pipeline runtime (Step Functions ExecutionTime).")
    parser.add_argument("--dashboard-name", default=os.environ.get("PIPELINE_DASHBOARD_NAME", "geyser-pipeline-runtime"),
                        help="CloudWatch dashboard name to create/update (default: geyser-pipeline-runtime).")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-2"),
                        help="AWS region (default: eu-west-2 or $AWS_REGION).")
    parser.add_argument("--state-machine-arn", default=os.environ.get("SFN_ARN"),
                        help="Step Functions State Machine ARN (or set SFN_ARN env var).")
    parser.add_argument("--lookback", default="-P14D",
                        help="Default time range shown in dashboard (e.g. -P3D, -P14D).")
    parser.add_argument("--dry-run", action="store_true", help="Print dashboard JSON and exit.")
    args = parser.parse_args()

    if not args.state_machine_arn:
        raise SystemExit("FATAL: Provide --state-machine-arn or export SFN_ARN")

    cw = boto3.client("cloudwatch", region_name=args.region)
    body = build_dashboard_body(args.region, args.state_machine_arn, args.lookback)

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return

    cw.put_dashboard(DashboardName=args.dashboard_name, DashboardBody=json.dumps(body))
    print(f"SUCCESS: Upserted dashboard '{args.dashboard_name}' in {args.region}")


if __name__ == "__main__":
    main()
