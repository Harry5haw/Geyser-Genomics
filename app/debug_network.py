# app/debug_network.py

import os
import subprocess
import logging
import boto3

# --- 1. Enable Boto3 Debug Logging ---
# This MUST be done before the client is initialized.
print("--- ENABLING BOTO3 DEBUG LOGGING ---")
boto3.set_stream_logger('botocore', level=logging.DEBUG)

# --- Configuration ---
AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
CW_ENDPOINT = f"monitoring.{AWS_REGION}.amazonaws.com"

def run_command(command):
    """Helper function to run a shell command and print its output."""
    print(f"\n--- RUNNING COMMAND: {' '.join(command)} ---")
    try:
        # We use capture_output=True to get stdout/stderr
        # We use text=True to decode them as strings
        # We use check=False so that a non-zero exit code doesn't crash our script
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30 # 30 second timeout to detect network hangs
        )
        print("--- STDOUT ---")
        print(result.stdout if result.stdout else "[No stdout]")
        print("--- STDERR ---")
        print(result.stderr if result.stderr else "[No stderr]")
        print(f"--- EXIT CODE: {result.returncode} ---")
    except subprocess.TimeoutExpired:
        print("--- COMMAND TIMED OUT AFTER 30 SECONDS ---")
    except Exception as e:
        print(f"--- COMMAND FAILED WITH EXCEPTION: {e} ---")

def main():
    print("--- STARTING NETWORK DIAGNOSTIC SCRIPT ---")

    # --- 2. Test TLS Handshake to CloudWatch Endpoint ---
    # The '-brief' flag simplifies output. We add '|| true' so the command
    # doesn't exit with an error code if the connection fails.
    openssl_command = [
        "openssl", "s_client",
        "-connect", f"{CW_ENDPOINT}:443",
        "-servername", CW_ENDPOINT,
        "-brief"
    ]
    run_command(openssl_command)
    
    # --- 3. Test AWS CLI PutMetricData ---
    # This tests the raw network and IAM auth from the container environment.
    aws_cli_command = [
        "aws", "cloudwatch", "put-metric-data",
        "--namespace", "TerraFlow/Debug",
        "--metric-name", "NetworkTest",
        "--value", "1",
        "--region", AWS_REGION
    ]
    run_command(aws_cli_command)

    # --- 4. Test Boto3 PutMetricData with Debug Logging ---
    # This tests our application's exact code path.
    print("\n--- RUNNING BOTO3 PUT-METRIC-DATA CALL ---")
    try:
        cloudwatch_client = boto3.client('cloudwatch', region_name=AWS_REGION)
        cloudwatch_client.put_metric_data(
            Namespace="TerraFlow/Debug",
            MetricData=[
                {
                    'MetricName': 'Boto3Test',
                    'Value': 1,
                    'Unit': 'Count'
                }
            ]
        )
        print("--- BOTO3 CALL SUCCEEDED (This is unexpected!) ---")
    except Exception as e:
        print(f"--- BOTO3 CALL FAILED WITH EXCEPTION ---")
        print(e)

    print("\n--- NETWORK DIAGNOSTIC SCRIPT COMPLETE ---")

if __name__ == "__main__":
    main()
