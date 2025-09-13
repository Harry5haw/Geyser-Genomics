# app/debug_canary.py

import boto3
import os
import logging

# Enable Boto3 debug logging to see the raw request.
print("--- CANARY SCRIPT: ENABLING BOTO3 DEBUG LOGGING ---")
boto3.set_stream_logger('botocore', level=logging.DEBUG)

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
CANARY_NAMESPACE = "TerraFlow/Canary"

def main():
    print(f"--- CANARY SCRIPT: ATTEMPTING TO SEND HARDCODED METRIC TO {AWS_REGION} ---")

    try:
        cloudwatch_client = boto3.client('cloudwatch', region_name=AWS_REGION)
        
        # This is the simplest possible valid metric. All values are hardcoded.
        response = cloudwatch_client.put_metric_data(
            Namespace=CANARY_NAMESPACE,
            MetricData=[
                {
                    'MetricName': 'CanaryTest',
                    'Dimensions': [
                        {
                            'Name': 'TestRun',
                            'Value': 'Success'
                        },
                    ],
                    'Value': 1.0, # Use a clean, simple float.
                    'Unit': 'Count'
                }
            ]
        )
        
        print("--- CANARY SCRIPT: BOTO3 CALL SUCCEEDED ---")
        print("--- RESPONSE METADATA ---")
        print(response.get('ResponseMetadata'))

    except Exception as e:
        print(f"--- CANARY SCRIPT: BOTO3 CALL FAILED WITH EXCEPTION ---")
        print(e)

    print("--- CANARY SCRIPT COMPLETE ---")

if __name__ == "__main__":
    main()
