# lambda/trigger/index.py
"""S3 upload -> Step Functions execution.

The parsing logic lives in pure functions (parse_raw_key, parse_sample_id, mate_key,
execution_name, build_pipeline_input) so scripts/run_local.py, the web UI and the tests use
exactly what the cloud trigger uses.

Single-end: raw_reads/<id>.fastq.gz starts a run straight away.
Paired-end: raw_reads/<id>_R1.fastq.gz and <id>_R2.fastq.gz arrive as two S3 events. Each event
checks whether the other mate exists; only once both do is a run started. If both land at once,
both events see both files: they start the execution under the same name (derived from the two
ETags), and Step Functions (STANDARD) lets only one exist, so the sample runs exactly once.
This needs s3:GetObject on raw_reads/* and s3:ListBucket (so a missing mate is a 404, not a 403).
"""

import hashlib
import json
import logging
import os
import re
import urllib.parse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_REFERENCE_GENOME = "chr20.fa"
MATES = ("R1", "R2")  # must match app/tasks.py (tests/test_consistency.py checks)
NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound")

_sfn_client = None
_s3_client = None


def parse_raw_key(object_key: str):
    """'raw_reads/X.fastq.gz' -> ('X', None); 'raw_reads/X_R1.fastq.gz' -> ('X', 'R1').

    The stem is everything before the first dot of the filename. Only _R1/_R2 mark a mate:
    SRA-style _1/_2 do not, so a single-end sample called run_1 stays 'run_1'.
    """
    stem = os.path.basename(object_key).split(".")[0]
    for mate in MATES:
        suffix = f"_{mate}"
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[:-len(suffix)], mate
    if not stem:
        raise ValueError(f"Could not parse a sample ID from object key {object_key!r}")
    return stem, None


def parse_sample_id(object_key: str) -> str:
    """'raw_reads/SRR062634.fastq.gz' or 'raw_reads/SRR062634_R1.fastq.gz' -> 'SRR062634'."""
    return parse_raw_key(object_key)[0]


def mate_key(object_key: str) -> str:
    """The other mate's key: raw_reads/X_R1.fastq.gz <-> raw_reads/X_R2.fastq.gz."""
    sample_id, mate = parse_raw_key(object_key)
    if mate is None:
        raise ValueError(f"{object_key!r} is not a paired-end mate (_R1/_R2)")
    other = MATES[1 - MATES.index(mate)]
    directory, filename = os.path.split(object_key)
    renamed = f"{sample_id}_{other}" + filename[len(sample_id) + 1 + len(mate):]
    return f"{directory}/{renamed}" if directory else renamed


def execution_name(sample_id: str, etags) -> str:
    """Deterministic Step Functions execution name for one upload of a pair.

    Same files -> same name (so two simultaneous events start one run); re-uploading changed
    files -> new ETags -> new name -> a new run. Names allow [A-Za-z0-9_-], at most 80 chars.
    """
    digest = hashlib.sha256("|".join(e.strip('"') for e in etags).encode()).hexdigest()[:12]
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", sample_id)[:64]
    return f"{safe_id}-{digest}"


def build_pipeline_input(object_key: str, reference_name: str = None) -> dict:
    """The exact input document the state machine expects (the same for single- and paired-end)."""
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


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3")
    return _s3_client


def _error_code(err) -> str:
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code", "")


def _other_mate_etag(bucket: str, key: str):
    """The other mate's ETag, or None if it hasn't been uploaded yet. Any other error propagates."""
    try:
        return _s3().head_object(Bucket=bucket, Key=key)["ETag"]
    except Exception as err:  # noqa: BLE001 - botocore ClientError, told apart by its code
        if _error_code(err) in NOT_FOUND_CODES:
            return None
        raise


def _response(status: int, **body) -> dict:
    return {"statusCode": status, "body": json.dumps(body)}


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
        etag = s3_record["object"].get("eTag", "")
    except (KeyError, IndexError) as e:
        logger.error(f"Failed to parse S3 event record: {e}")
        raise RuntimeError("Could not parse S3 event.") from e

    pipeline_input = build_pipeline_input(object_key)
    sample_id = pipeline_input["srr_id"]
    _, mate = parse_raw_key(object_key)
    start_args = {"stateMachineArn": state_machine_arn, "input": json.dumps(pipeline_input)}

    if mate is not None:
        other_key = mate_key(object_key)
        other_etag = _other_mate_etag(bucket_name, other_key)
        if other_etag is None:
            logger.info(f"'{sample_id}': {mate} arrived, waiting for s3://{bucket_name}/{other_key}")
            return _response(202, waiting_for=other_key)
        etags = {mate: etag, parse_raw_key(other_key)[1]: other_etag}
        start_args["name"] = execution_name(sample_id, [etags[m] for m in MATES])

    logger.info(f"Triggering pipeline for '{sample_id}' from s3://{bucket_name}/{object_key}")
    try:
        response = _sfn().start_execution(**start_args)
    except Exception as err:  # noqa: BLE001 - botocore ClientError, told apart by its code
        if _error_code(err) == "ExecutionAlreadyExists":
            # The other mate's event got there first under the same name: the sample is already running.
            logger.info(f"'{sample_id}': execution {start_args.get('name')} already started")
            return _response(200, already_started=start_args.get("name"))
        raise
    logger.info(f"## Started Step Function execution: {response['executionArn']}")
    return _response(200, executionArn=response["executionArn"])
