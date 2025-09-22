#!/usr/bin/env python3
"""
Deploy the official CloudWatch dashboard for Geyser Genomics.

This script overwrites (or creates) the `teraflow-dashboard-dev` dashboard with the
proven aggregation widget that shows average durations for each pipeline stage.

- Uses the working JSON from `teraflow-aggregated-python`
- Defaults to 2 weeks view (`-P14D`) so you can see older runs
- One widget only (the aggregated task durations)

Run:
  python scripts/deploy_dashboard.py
"""

import boto3
import json
import sys
import argparse

DEFAULT_DASHBOARD_NAME = "teraflow-dashboard-dev"
DEFAULT_REGION = "eu-west-2"
DEFAULT_NAMESPACE = "TerraFlowGenomics"

def build_dashboard_body(region: str, namespace: str) -> str:
    """
    Returns the JSON for a single-widget dashboard.
    """
    dashboard_body = {
        "start": "-P14D",  # default time range = past 14 days
        "periodOverride": "inherit",
        "widgets": [
            {
                "type": "metric",
                "x": 0,
                "y": 0,
                "width": 16,
                "height": 8,
                "properties": {
                    "view": "timeSeries",
                    "stacked": False,
                    "region": region,
                    "title": "Task Duration - Aggregated by Task Type",
                    "period": 300,
                    "stat": "Average",
                    "metrics": [
                        [
                            {
                                "expression": f"AVG(SEARCH('{{{namespace},SampleId,Status,TaskName}} MetricName=\"Duration\" TaskName=\"Decompress\"', 'Average'))",
                                "id": "decompress_avg",
                                "label": "Decompress (Average)"
                            }
                        ],
                        [
                            {
                                "expression": f"AVG(SEARCH('{{{namespace},SampleId,Status,TaskName}} MetricName=\"Duration\" TaskName=\"QualityControl\"', 'Average'))",
                                "id": "qc_avg",
                                "label": "QualityControl (Average)"
                            }
                        ],
                        [
                            {
                                "expression": f"AVG(SEARCH('{{{namespace},SampleId,Status,TaskName}} MetricName=\"Duration\" TaskName=\"Align\"', 'Average'))",
                                "id": "align_avg",
                                "label": "Align (Average)"
                            }
                        ],
                        [
                            {
                                "expression": f"AVG(SEARCH('{{{namespace},SampleId,Status,TaskName}} MetricName=\"Duration\" TaskName=\"CallVariants\"', 'Average'))",
                                "id": "variants_avg",
                                "label": "CallVariants (Average)"
                            }
                        ]
                    ],
                    "yAxis": {
                        "left": {
                            "min": 0,
                            "label": "Duration (seconds)"
                        }
                    }
                }
            }
        ]
    }
    return json.dumps(dashboard_body)

def deploy_dashboard(dashboard_name: str, region: str, namespace: str, dry_run: bool = False) -> None:
    body = build_dashboard_body(region, namespace)

    if dry_run:
        print(json.dumps(json.loads(body), indent=2))
        return

    client = boto3.client("cloudwatch", region_name=region)
    try:
        resp = client.put_dashboard(
            DashboardName=dashboard_name,
            DashboardBody=body
        )
    except Exception as e:
        print(f"[ERROR] Failed to put dashboard: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.get("DashboardValidationMessages"):
        print("[WARNING] Dashboard validation messages:")
        for m in resp["DashboardValidationMessages"]:
            print(f" - {m}")
    else:
        print(f"[OK] Dashboard '{dashboard_name}' deployed in {region}.")

def parse_args():
    p = argparse.ArgumentParser(description="Deploy CloudWatch dashboard for Geyser Genomics.")
    p.add_argument("--dashboard-name", default=DEFAULT_DASHBOARD_NAME, help="Name of the dashboard to create/update")
    p.add_argument("--region", default=DEFAULT_REGION, help="AWS region")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="Metric namespace (default: TerraFlowGenomics)")
    p.add_argument("--dry-run", action="store_true", help="Print JSON without deploying")
    return p.parse_args()

def main():
    args = parse_args()
    deploy_dashboard(args.dashboard_name, args.region, args.namespace, args.dry_run)

if __name__ == "__main__":
    main()
