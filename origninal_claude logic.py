#!/usr/bin/env python3
"""
Alternative approach using Math expressions to aggregate SEARCH results
"""

import boto3
import json

def create_aggregated_dashboard():
    """Try using math expressions to aggregate SEARCH results"""
    cloudwatch = boto3.client('cloudwatch', region_name='eu-west-2')
    
    dashboard_name = "teraflow-aggregated-python"
    
    dashboard_body = {
        "start": "-PT24H",
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
                    "region": "eu-west-2",
                    "title": "Task Duration - Aggregated by Task Type",
                    "period": 300,
                    "stat": "Average",
                    "metrics": [
                        # Search for all Decompress tasks and average them
                        [
                            {
                                "expression": "AVG(SEARCH('{TerraFlowGenomics,SampleId,Status,TaskName} MetricName=\"Duration\" TaskName=\"Decompress\"', 'Average'))",
                                "id": "decompress_avg",
                                "label": "Decompress (Average)"
                            }
                        ],
                        [
                            {
                                "expression": "AVG(SEARCH('{TerraFlowGenomics,SampleId,Status,TaskName} MetricName=\"Duration\" TaskName=\"QualityControl\"', 'Average'))",
                                "id": "qc_avg", 
                                "label": "QualityControl (Average)"
                            }
                        ],
                        [
                            {
                                "expression": "AVG(SEARCH('{TerraFlowGenomics,SampleId,Status,TaskName} MetricName=\"Duration\" TaskName=\"Align\"', 'Average'))",
                                "id": "align_avg",
                                "label": "Align (Average)"
                            }
                        ],
                        [
                            {
                                "expression": "AVG(SEARCH('{TerraFlowGenomics,SampleId,Status,TaskName} MetricName=\"Duration\" TaskName=\"CallVariants\"', 'Average'))",
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
    
    try:
        response = cloudwatch.put_dashboard(
            DashboardName=dashboard_name,
            DashboardBody=json.dumps(dashboard_body)
        )
        
        print(f"✅ Aggregated dashboard '{dashboard_name}' created successfully!")
        print(f"URL: https://eu-west-2.console.aws.amazon.com/cloudwatch/home?region=eu-west-2#dashboards:name={dashboard_name}")
        return True
        
    except Exception as e:
        print(f"❌ Error creating aggregated dashboard: {str(e)}")
        return False

def create_simple_grouped_dashboard():
    """Try a completely different approach - maybe we need to avoid SEARCH entirely"""
    cloudwatch = boto3.client('cloudwatch', region_name='eu-west-2')
    
    dashboard_name = "teraflow-simple-grouped"
    
    # Let's try manually specifying a few key pipeline runs and see if that groups better
    dashboard_body = {
        "start": "-PT24H",
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
                    "region": "eu-west-2",
                    "title": "Manual Grouping Test - Recent Pipeline Runs",
                    "period": 300,
                    "stat": "Average",
                    "metrics": [
                        # Decompress tasks from multiple recent runs
                        ["TerraFlowGenomics", "Duration", "SampleId", "small_testV25", "Status", "Success", "TaskName", "Decompress", {"label": "Decompress"}],
                        [".", ".", ".", "small_testV24", ".", ".", ".", ".", {"label": "Decompress"}], 
                        [".", ".", ".", "small_testV23", ".", ".", ".", ".", {"label": "Decompress"}],
                        
                        # QualityControl tasks
                        [".", ".", ".", "small_testV25", ".", ".", ".", "QualityControl", {"label": "QualityControl"}],
                        [".", ".", ".", "small_testV24", ".", ".", ".", ".", {"label": "QualityControl"}],
                        [".", ".", ".", "small_testV23", ".", ".", ".", ".", {"label": "QualityControl"}],
                        
                        # Align tasks  
                        [".", ".", ".", "small_testV25", ".", ".", ".", "Align", {"label": "Align"}],
                        [".", ".", ".", "small_testV24", ".", ".", ".", ".", {"label": "Align"}],
                        [".", ".", ".", "small_testV23", ".", ".", ".", ".", {"label": "Align"}],
                        
                        # CallVariants tasks
                        [".", ".", ".", "small_testV25", ".", ".", ".", "CallVariants", {"label": "CallVariants"}],
                        [".", ".", ".", "small_testV24", ".", ".", ".", ".", {"label": "CallVariants"}],
                        [".", ".", ".", "small_testV23", ".", ".", ".", ".", {"label": "CallVariants"}]
                    ]
                }
            }
        ]
    }
    
    try:
        response = cloudwatch.put_dashboard(
            DashboardName=dashboard_name,
            DashboardBody=json.dumps(dashboard_body)
        )
        
        print(f"✅ Simple grouped dashboard '{dashboard_name}' created!")
        print(f"URL: https://eu-west-2.console.aws.amazon.com/cloudwatch/home?region=eu-west-2#dashboards:name={dashboard_name}")
        return True
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

if __name__ == "__main__":
    print("Testing Different Aggregation Approaches")
    print("=" * 45)
    
    print("\n1. Trying Math expressions with AVG()...")
    math_success = create_aggregated_dashboard()
    
    print("\n2. Trying manual grouping with labels...")
    manual_success = create_simple_grouped_dashboard()
    
    if math_success or manual_success:
        print("\n✅ Created test dashboards!")
        print("Check both dashboards to see which approach works better for grouping.")
    else:
        print("\n❌ Both approaches failed. The issue might be deeper.")