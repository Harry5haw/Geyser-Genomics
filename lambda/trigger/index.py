# lambda/trigger/index.py
"""S3 upload -> Step Functions execution.

The parsing logic lives in pure functions (parse_sample_id, build_pipeline_input)
so scripts/run_local.py and the tests use exactly what the cloud trigger uses.
"""

import hashlib
import json
import logging
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_REFERENCE_GENOME = "chr20.fa"

# Execution names: 1-80 characters. Kept to [A-Za-z0-9_-] because the name is reused in the Batch job
# names (letters, digits, hyphens, underscores; max 128) and non-ASCII names break CloudWatch logging.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
MAX_SAMPLE_PART = 30

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


def build_execution_name(sample_id: str, event_time: str = None, event_key: str = None) -> str:
    """'SRR062634' -> 'SRR062634-20261101T120000Z-1a2b3c4d': readable in the Step Functions console.

    event_time is the S3 record's eventTime (UTC). event_key identifies the S3 event (bucket, key and
    sequencer); its hash makes the name the same for a duplicate delivery of the same event, so Step
    Functions does not start a second run. Without it a random suffix is used.
    """
    sample = _UNSAFE_NAME_CHARS.sub("-", sample_id)[:MAX_SAMPLE_PART] or "sample"
    try:
        when = datetime.strptime(event_time[:19], "%Y-%m-%dT%H:%M:%S") if event_time else None
    except ValueError:
        when = None
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    suffix = hashlib.sha1(event_key.encode("utf-8")).hexdigest()[:8] if event_key else uuid.uuid4().hex[:8]
    return f"{sample}-{stamp}-{suffix}"


def _is_already_exists(err: Exception) -> bool:
    code = getattr(err, "response", {}).get("Error", {}).get("Code")
    return code == "ExecutionAlreadyExists" or type(err).__name__ == "ExecutionAlreadyExists"


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
        record = event["Records"][0]
        s3_record = record["s3"]
        bucket_name = s3_record["bucket"]["name"]
        object_key = urllib.parse.unquote_plus(s3_record["object"]["key"], encoding="utf-8")
    except (KeyError, IndexError) as e:
        logger.error(f"Failed to parse S3 event record: {e}")
        raise RuntimeError("Could not parse S3 event.") from e

    pipeline_input = build_pipeline_input(object_key)
    logger.info(f"Triggering pipeline for '{pipeline_input['srr_id']}' from s3://{bucket_name}/{object_key}")

    sequencer = s3_record["object"].get("sequencer")
    name = build_execution_name(
        pipeline_input["srr_id"], record.get("eventTime"),
        f"{bucket_name}/{object_key}/{sequencer}" if sequencer else None)

    try:
        response = _sfn().start_execution(stateMachineArn=state_machine_arn, name=name,
                                          input=json.dumps(pipeline_input))
    except Exception as err:
        if not _is_already_exists(err):
            raise
        # Same S3 event delivered again, and its run has already finished: nothing to do.
        logger.warning(f"## Duplicate S3 event: execution '{name}' already exists, not starting another")
        return {"statusCode": 200, "body": json.dumps({"executionName": name, "duplicate": True})}
    logger.info(f"## Started Step Function execution '{name}': {response['executionArn']}")
    return {"statusCode": 200, "body": json.dumps({"executionArn": response["executionArn"], "executionName": name})}
