#!/usr/bin/env python3
"""
Manages the Geyser Genomics CloudWatch dashboard.
This script is called by Terraform to create, update, or destroy the dashboard.
Its logic is based on the proven 'create_aggregated_dashboard' function.
"""
import boto3
import json
import sys
import os

def main():
    """
    Manages the CloudWatch dashboard based on the provided action.
    Reads configuration from environment variables set by Terraform.
    """
    if len(sys.argv) < 2 or sys.argv[1] not in ["create", "destroy"]:
        print("Usage: python manage_dashboard.py [create|destroy]")
        sys.exit(1)

    action = sys.argv[1]
    
    dashboard_name = os.environ.get("DASHBOARD_NAME")
    region = os.environ.get("AWS_REGION")
    state_machine_arn = os.environ.get("SFN_ARN")

    if not all([dashboard_name, region, state_machine_arn]):
        print("ERROR: Required environment variables (DASHBOARD_NAME, AWS_REGION, SFN_ARN) are not set.")
        sys.exit(1)

    cloudwatch = boto3.client('cloudwatch', region_name=region)

    if action == "create":
        print(f"Creating/Updating dashboard: {dashboard_name} in region {region}")
        
        dashboard_body = {
            "widgets": [
                {
                    "type": "metric", "x": 0, "y": 0, "width": 16, "height": 8,
                    "properties": {
                        "view": "timeSeries", "stacked": False, "region": region,
                        "title": "Average Task Duration (All Pipeline Runs)", "period": 300, "stat": "Average",
                        "metrics": [
                            [{"expression": "AVG(SEARCH('{GeyserGenomics,SampleId,TaskName} MetricName=\"Duration\" TaskName=\"Decompress\"', 'Average'))", "id": "decompress_avg", "label": "Decompress (Average)"}],
                            [{"expression": "AVG(SEARCH('{GeyserGenomics,SampleId,TaskName} MetricName=\"Duration\" TaskName=\"QualityControl\"', 'Average'))", "id": "qc_avg", "label": "QualityControl (Average)"}],
                            [{"expression": "AVG(SEARCH('{GeyserGenomics,SampleId,TaskName} MetricName=\"Duration\" TaskName=\"Align\"', 'Average'))", "id": "align_avg", "label": "Align (Average)"}],
                            [{"expression": "AVG(SEARCH('{GeyserGenomics,SampleId,TaskName} MetricName=\"Duration\" TaskName=\"CallVariants\"', 'Average'))", "id": "variants_avg", "label": "CallVariants (Average)"}]
                        ],
                        "yAxis": {"left": {"min": 0, "label": "Duration (seconds)"}}
                    }
                },
                {
                    "type": "metric", "x": 16, "y": 0, "width": 8, "height": 4,
                    "properties": {
                        "view": "singleValue", "region": region, "title": "Total Pipeline Executions",
                        "metrics": [["AWS/States", "ExecutionsStarted", "StateMachineArn", state_machine_arn]],
                        "stat": "Sum"
                    }
                }
            ]
        }

        try:
            cloudwatch.put_dashboard(DashboardName=dashboard_name, DashboardBody=json.dumps(dashboard_body))
            # CORRECTED: Replaced emoji with simple text for cross-platform compatibility.
            print("SUCCESS: Dashboard created/updated successfully.")
        except Exception as e:
            # CORRECTED: Replaced emoji with simple text.
            print(f"ERROR: Error creating dashboard: {e}")
            sys.exit(1)

    elif action == "destroy":
        print(f"Destroying dashboard: {dashboard_name}")
        try:
            cloudwatch.delete_dashboards(DashboardNames=[dashboard_name])
            # CORRECTED: Replaced emoji with simple text.
            print("SUCCESS: Dashboard destroyed successfully.")
        except Exception as e:
            # CORRECTED: Replaced emoji with simple text.
            print(f"ERROR: Error destroying dashboard: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
