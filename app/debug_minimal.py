# app/debug_minimal.py

import boto3
import os

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
MINIMAL_NAMESPACE = "TerraFlowMinimal" # No slashes

def main():
    print(f"--- MINIMAL SCRIPT: ATTEMPTING TO SEND MINIMAL METRIC TO {AWS_REGION} ---")
    try:
        cloudwatch_client = boto3.client('cloudwatch', region_name=AWS_REGION)
        
        # The simplest metric possible: no dimensions, integer value.
        response = cloudwatch_client.put_metric_data(
            Namespace=MINIMAL_NAMESPACE,
            MetricData=[
                {
                    'MetricName': 'MinimalViableTest',
                    'Value': 1,
                    'Unit': 'Count'
                }
            ]
        )
        print("--- MINIMAL SCRIPT: BOTO3 CALL SUCCEEDED ---")
        print("--- RESPONSE METADATA ---")
        print(response.get('ResponseMetadata'))
    except Exception as e:
        print(f"--- MINIMAL SCRIPT: BOTO3 CALL FAILED WITH EXCEPTION ---")
        print(e)
    print("--- MINIMAL SCRIPT COMPLETE ---")

if __name__ == "__main__":
    main()
