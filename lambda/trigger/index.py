# lambda/trigger/index.py
"""S3 upload -> Step Functions execution.

The parsing logic lives in pure functions (parse_raw_key, parse_sample_id, mate_key,
build_pipeline_input, build_execution_name, paired_execution_name) so scripts/run_local.py, the
web UI and the tests use exactly what the cloud trigger uses.

Single-end: raw_reads/<id>.fastq.gz starts a run straight away, named after the S3 event.
Paired-end: raw_reads/<id>_R1.fastq.gz and <id>_R2.fastq.gz arrive as two S3 events. Each event
checks whether both mates exist; only once they do is a run started. If both land at once, both
events see both files and derive the same execution name from the two objects (ETag and
LastModified), and Step Functions (STANDARD) lets only one execution have it, so the sample runs
exactly once. This needs s3:GetObject on raw_reads/* and s3:ListBucket (so a missing mate is a
404, not a 403).
"""

import hashlib
import json
import logging
import os
import re
import urllib.parse
import uuid
from datetime import UTC, datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_REFERENCE_GENOME = "chr20.fa"
MATES = ("R1", "R2")  # must match app/tasks.py (tests/test_consistency.py checks)
RAW_SUFFIX = ".fastq.gz"  # the raw key suffix tasks.keys_for reads
NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound")

# Execution names: 1-80 characters. Kept to [A-Za-z0-9_-] because the name is reused in the Batch job
# names (letters, digits, hyphens, underscores; max 128) and non-ASCII names break CloudWatch logging.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
MAX_SAMPLE_PART = 30

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
    """The other mate's key: raw_reads/X_R1.fastq.gz <-> raw_reads/X_R2.fastq.gz.

    Mates must use the exact names the stages read (tasks.keys_for): <id>_R1.fastq.gz, not .fq.gz.
    Anything else is refused here, loudly, rather than waiting forever for a partner whose name
    never matches or starting a run that can't find its reads.
    """
    sample_id, mate = parse_raw_key(object_key)
    if mate is None:
        raise ValueError(f"{object_key!r} is not a paired-end mate (_R1/_R2)")
    directory, filename = os.path.split(object_key)
    if filename != f"{sample_id}_{mate}{RAW_SUFFIX}":
        raise ValueError(f"Paired-end mates must be named <id>_R1{RAW_SUFFIX} / <id>_R2{RAW_SUFFIX}, "
                         f"got {filename!r}")
    other = MATES[1 - MATES.index(mate)]
    renamed = f"{sample_id}_{other}{RAW_SUFFIX}"
    return f"{directory}/{renamed}" if directory else renamed


def build_pipeline_input(object_key: str, reference_name: str = None) -> dict:
    """The exact input document the state machine expects (the same for single- and paired-end)."""
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
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    suffix = hashlib.sha1(event_key.encode("utf-8")).hexdigest()[:8] if event_key else uuid.uuid4().hex[:8]
    return f"{sample}-{stamp}-{suffix}"


def paired_execution_name(sample_id: str, versions) -> str:
    """Execution name for one upload of a pair, in the same format as build_execution_name.

    versions: one (ETag, LastModified) per mate, R1 first, as HeadObject reports them. Both mates'
    events read the same two objects, so they compute the same name and only one run starts. The
    time is the later upload; the hash covers both objects, so uploading the pair again (even with
    identical content: LastModified changes) gives a new name and a new run.
    """
    versions = [(str(etag).strip('"'), _iso(modified)) for etag, modified in versions]
    latest = max(modified for _, modified in versions)
    key = "|".join(f"{etag}@{modified}" for etag, modified in versions)
    return build_execution_name(sample_id, latest, key)


def _iso(value) -> str:
    """HeadObject's LastModified (a datetime) as '2026-11-01T20:00:05+00:00'."""
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace(" ", "T", 1)


def _error_code(err) -> str:
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code", "")


def _is_already_exists(err: Exception) -> bool:
    return _error_code(err) == "ExecutionAlreadyExists" or type(err).__name__ == "ExecutionAlreadyExists"


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


def _object_version(bucket: str, key: str):
    """(ETag, LastModified) of an object, or None if it doesn't exist. Any other error propagates."""
    try:
        head = _s3().head_object(Bucket=bucket, Key=key)
    except Exception as err:  # noqa: BLE001 - botocore ClientError, told apart by its code
        if _error_code(err) in NOT_FOUND_CODES:
            return None
        raise
    return head["ETag"], head["LastModified"]


def _response(status: int, **body) -> dict:
    return {"statusCode": status, "body": json.dumps(body)}


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
    sample_id = pipeline_input["srr_id"]
    _, mate = parse_raw_key(object_key)

    if mate is None:
        sequencer = s3_record["object"].get("sequencer")
        name = build_execution_name(
            sample_id, record.get("eventTime"),
            f"{bucket_name}/{object_key}/{sequencer}" if sequencer else None)
    else:
        other_key = mate_key(object_key)
        keys = {mate: object_key, parse_raw_key(other_key)[1]: other_key}
        # HEAD both mates (not just the other one) so that both events derive the name from the
        # same two (ETag, LastModified) pairs, whichever event runs first.
        versions = {m: _object_version(bucket_name, keys[m]) for m in MATES}
        missing = [m for m in MATES if versions[m] is None]
        if missing:
            waiting_for = keys[missing[0]]
            logger.info(f"'{sample_id}': {mate} arrived, waiting for s3://{bucket_name}/{waiting_for}")
            return _response(202, waiting_for=waiting_for)
        name = paired_execution_name(sample_id, [versions[m] for m in MATES])

    logger.info(f"Triggering pipeline for '{sample_id}' from s3://{bucket_name}/{object_key}")
    try:
        response = _sfn().start_execution(stateMachineArn=state_machine_arn, name=name,
                                          input=json.dumps(pipeline_input))
    except Exception as err:
        if not _is_already_exists(err):
            raise
        # The same S3 event delivered again, or the other mate's event got there first under the
        # same name: the sample is already running (or ran). Nothing to do.
        logger.warning(f"## Execution '{name}' already exists, not starting another")
        return _response(200, executionName=name, duplicate=True)
    logger.info(f"## Started Step Function execution '{name}': {response['executionArn']}")
    return _response(200, executionArn=response["executionArn"], executionName=name)
