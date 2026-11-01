# lambda/trigger/index.py
"""S3 upload -> Step Functions execution.

The parsing logic lives in pure functions (parse_sample_id, build_pipeline_input)
so scripts/run_local.py and the tests use exactly what the cloud trigger uses.
"""

import json
import logging
import os
import urllib.parse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_REFERENCE_GENOME = "chr20.fa"

_sfn_client = None


def parse_sample_id(object_key: str) -> str:
    """'raw_reads/SRR062634.fastq.gz' -> 'SRR062634' (everything before the first dot of the filename)."""
    sample_id = os.path.basename(object_key).split(".")[0]
    if not sample_id:
        raise ValueError(f"Could not parse a sample ID from object key {object_key!r}")
    return sample_id


def build_pipeline_input(object_key: str, reference_name: str = None) -> dict:
    """The exact input document the state machine expects."""
    return {
        "srr_id": parse_sample_id(object_key),
        "reference_name": reference_name or os.environ.get("REFERENCE_NAME", DEFAULT_REFERENCE_GENOME),
    }


def _sfn():
    global _sfn_client
    if _sfn_client is None:
        import boto3

        _sfn_client = boto3.client("stepfunctions")
    return _sfn_client


def handler(event, context):
    logger.info("## EVENT RECEIVED")
    logger.info(json.dumps(event))

    state_machine_arn = os.environ.get("STATE_MACHINE_ARN")
    if not state_machine_arn:
        raise ValueError("STATE_MACHINE_ARN environment variable not set.")

    try:
        s3_record = event["Records"][0]["s3"]
        bucket_name = s3_record["bucket"]["name"]
        object_key = urllib.parse.unquote_plus(s3_record["object"]["key"], encoding="utf-8")
    except (KeyError, IndexError) as e:
        logger.error(f"Failed to parse S3 event record: {e}")
        raise RuntimeError("Could not parse S3 event.") from e

    pipeline_input = build_pipeline_input(object_key)
    logger.info(f"Triggering pipeline for '{pipeline_input['srr_id']}' from s3://{bucket_name}/{object_key}")

    response = _sfn().start_execution(stateMachineArn=state_machine_arn, input=json.dumps(pipeline_input))
    logger.info(f"## Started Step Function execution: {response['executionArn']}")
    return {"statusCode": 200, "body": json.dumps({"executionArn": response["executionArn"]})}
