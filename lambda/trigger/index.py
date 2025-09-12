# lambda/trigger/index.py

import json
import logging
import os
import urllib.parse

import boto3

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
sfn_client = boto3.client("stepfunctions")

# Fetch environment variables
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN")
if not STATE_MACHINE_ARN:
    raise ValueError("STATE_MACHINE_ARN environment variable not set.")

# This can be configured as an env var as well for more flexibility
DEFAULT_REFERENCE_GENOME = "chr20.fa"


def handler(event, context):
    """
    Lambda handler triggered by an S3 object creation event.

    This function parses the S3 event to extract the sample ID (srr_id),
    constructs the specific input payload required by the TerraFlow Step Function,
    and starts a new execution.
    """
    logger.info("## EVENT RECEIVED")
    logger.info(json.dumps(event))

    try:
        s3_record = event["Records"][0]["s3"]
        bucket_name = s3_record["bucket"]["name"]
        object_key = urllib.parse.unquote_plus(s3_record["object"]["key"], encoding='utf-8')
    except (KeyError, IndexError) as e:
        logger.error(f"Failed to parse S3 event record: {e}")
        raise RuntimeError("Could not parse S3 event.") from e

    # --- CRITICAL LOGIC: Extract srr_id from the object key ---
    # Example object_key: 'raw_reads/small_test.fastq.gz'
    # We want to extract 'small_test'
    try:
        filename = os.path.basename(object_key)
        # Split on the first dot to handle extensions like .fastq.gz
        srr_id = filename.split('.')[0]
        if not srr_id:
            raise ValueError("Parsed srr_id is empty.")
    except Exception as e:
        logger.error(f"Could not parse srr_id from object key: '{object_key}'. Error: {e}")
        raise ValueError(f"Invalid filename format for parsing SRR ID.") from e

    logger.info(f"Triggering pipeline for srr_id: '{srr_id}' from file s3://{bucket_name}/{object_key}")

    # --- CRITICAL LOGIC: Construct the exact input payload for Step Functions ---
    pipeline_input = {
        "srr_id": srr_id,
        "reference_name": DEFAULT_REFERENCE_GENOME
    }

    try:
        response = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            input=json.dumps(pipeline_input)
        )
        logger.info(f"## Started Step Function execution: {response['executionArn']}")

        return {
            "statusCode": 200,
            "body": json.dumps({"executionArn": response["executionArn"]})
        }

    except Exception as e:
        logger.error(f"Failed to start Step Function execution: {e}")
        raise

