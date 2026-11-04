# lambda/web_api/index.py
"""The hosted web UI's small API (a Lambda function URL, auth NONE: the checks are here).

The site itself is static HTML on S3 behind CloudFront (infrastructure/web/). This function is the only
dynamic part:

  POST /uploads           token  start a presigned multipart upload to raw_reads/ (one file, or both mates),
                                 with the run's settings as S3 metadata (gate-profile, reference, annotate);
                                 the browser PUTs the parts straight to S3
  POST /uploads/complete  token  finish one file's multipart upload (the S3 trigger then starts the run)
  POST /uploads/abort     token  drop an unfinished upload
  GET  /status/<id>              the sample's latest run: status and times, as a small JSON
  GET  /files/<id>/<kind>        a 302 to a short-lived presigned GET, for a keys_for() output only

Uploads need the token: a long random string in an SSM SecureString (named by UPLOAD_TOKEN_PARAMETER),
read once per cold start and compared in constant time. It is never logged, and neither is any request
header or body: the logs say only the method, the route and the response status.

The settings are passed on as uploaded, like the trigger does: the state machine is the one validator
(Check_Gate_Profile, Check_Annotate, Check_Reference_Name), so a bad value fails the run visibly. Here only
the shape is checked (known keys, short printable strings).
"""

import base64
import hmac
import json
import logging
import math
import os
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MATES = ("R1", "R2")  # must match app/tasks.py and the trigger (tests/test_web_api.py checks)
RAW_SUFFIX = ".fastq.gz"
# The output files a page may link to: tasks.keys_for() minus the raw reads (the uploads stay private).
# Templates, so the Lambda zip needs nothing from app/; tests/test_web_api.py checks them against keys_for.
FILE_KEY_TEMPLATES = {
    "qc_html": "qc_reports/{id}.fastp.html",
    "qc_json": "qc_reports/{id}.fastp.json",
    "cram": "alignments/{id}.cram",
    "crai": "alignments/{id}.cram.crai",
    "bam": "alignments/{id}.bam",
    "vcf": "variants/{id}.vcf.gz",
    "tbi": "variants/{id}.vcf.gz.tbi",
    "csq_vcf": "variants/{id}.csq.vcf.gz",
    "csq_tbi": "variants/{id}.csq.vcf.gz.tbi",
    "flagstat": "stats/{id}/flagstat.txt",
    "mosdepth_summary": "stats/{id}/mosdepth.summary.txt",
    "mosdepth_dist": "stats/{id}/mosdepth.global.dist.txt",
    "bcftools_stats": "stats/{id}/bcftools.stats.txt",
    "gate_verdict": "qc_reports/{id}.gate.json",
}
# Upload metadata the run reads (lambda/trigger/index.py): S3 user metadata keys, as the trigger spells them.
METADATA_KEYS = ("gate-profile", "reference", "annotate")
MAX_METADATA_VALUE = 100

# A sample id the pipeline handles everywhere unchanged: it is the execution name's first part (the trigger
# keeps [A-Za-z0-9_-], at most 30 characters) and the stem of every key. _R1/_R2 would look like a mate.
SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$")
MATE_LIKE = re.compile(r"_R[12]$")
# Execution names: <sample>-<UTC time>-<8 hex> (trigger.build_execution_name).
RUN_NAME_TAIL = re.compile(r"^-\d{8}T\d{6}Z-[0-9a-f]{8}$")
STATUS_PAGES = 3  # list_executions pages (100 runs each, newest first) searched for a sample's latest run

MiB = 1024 * 1024
S3_MAX_PARTS = 10_000
S3_MIN_PART = 5 * MiB

_clients = {}
_token = None


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _client(service: str):
    if service not in _clients:
        import boto3
        from botocore.config import Config

        # Regional, virtual-hosted, SigV4 URLs (https://<bucket>.s3.<region>.amazonaws.com/...): the origin
        # the data lake's CORS rule and the site's CSP allow.
        config = Config(signature_version="s3v4", s3={"addressing_style": "virtual"}) if service == "s3" else None
        _clients[service] = boto3.client(service, config=config)
    return _clients[service]


def _upload_token() -> str:
    """The upload token from SSM, once per cold start. Any failure is a 503, saying nothing about why."""
    global _token
    if _token is None:
        name = os.environ.get("UPLOAD_TOKEN_PARAMETER")
        if not name:
            raise HttpError(503, "uploads are not configured")
        try:
            value = _client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except Exception as err:  # noqa: BLE001 - logged by type only; the value is never logged
            logger.error(f"could not read the upload token parameter: {type(err).__name__}")
            raise HttpError(503, "uploads are unavailable") from None
        if len(value) < 32:  # a short token is a misconfiguration, not a secret worth checking against
            logger.error("the upload token parameter is shorter than 32 characters: uploads disabled")
            raise HttpError(503, "uploads are unavailable")
        _token = value
    return _token


def _check_token(headers: dict) -> None:
    supplied = headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        raise HttpError(401, "an upload token is required (Authorization: Bearer <token>)")
    expected = _upload_token()
    if not hmac.compare_digest(supplied[len("Bearer ") :].encode("utf-8"), expected.encode("utf-8")):
        raise HttpError(401, "the upload token is not valid")


def _json_body(event: dict) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "the body is not valid base64 UTF-8") from None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HttpError(400, "the body must be JSON") from None
    if not isinstance(data, dict):
        raise HttpError(400, "the body must be a JSON object")
    return data


def _sample_id(value) -> str:
    if not isinstance(value, str) or not SAMPLE_ID.match(value) or MATE_LIKE.search(value):
        raise HttpError(
            400, "sample_id must be 1-30 of A-Z a-z 0-9 _ - (starting with a letter or digit, not ending _R1/_R2)"
        )
    return value


def raw_keys(sample_id: str, layout: str) -> list:
    """The raw read keys of an upload, as tasks.keys_for names them (single, or R1 then R2)."""
    if layout == "single":
        return [f"raw_reads/{sample_id}{RAW_SUFFIX}"]
    if layout == "paired":
        return [f"raw_reads/{sample_id}_{mate}{RAW_SUFFIX}" for mate in MATES]
    raise HttpError(400, "layout must be single or paired")


def _metadata(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HttpError(400, "metadata must be an object")
    unknown = sorted(set(value) - set(METADATA_KEYS))
    if unknown:
        raise HttpError(400, f"unknown metadata {unknown}: only {list(METADATA_KEYS)}")
    for key, item in value.items():
        if not isinstance(item, str) or len(item) > MAX_METADATA_VALUE or not item.isprintable():
            raise HttpError(
                400, f"metadata {key} must be a printable string of at most {MAX_METADATA_VALUE} characters"
            )
    return dict(value)


def part_size_for(size: int, preferred: int) -> int:
    """The part size: the preferred one, unless the file needs bigger parts to stay within 10,000 (whole MiB)."""
    needed = math.ceil(size / S3_MAX_PARTS)
    return max(preferred, S3_MIN_PART, math.ceil(needed / MiB) * MiB)


def start_upload(event: dict) -> tuple:
    _check_token(event["headers"])
    data = _json_body(event)
    sample_id = _sample_id(data.get("sample_id"))
    keys = raw_keys(sample_id, data.get("layout", "single"))
    sizes = data.get("sizes")
    max_bytes = _env_int("MAX_UPLOAD_BYTES", 64 * 1024 * MiB)
    if (
        not isinstance(sizes, list)
        or len(sizes) != len(keys)
        or not all(isinstance(s, int) and not isinstance(s, bool) and 0 < s <= max_bytes for s in sizes)
    ):
        raise HttpError(400, f"sizes must list one size in bytes per file ({len(keys)}), each 1 to {max_bytes}")
    metadata = _metadata(data.get("metadata"))
    expires = _env_int("UPLOAD_URL_SECONDS", 3600)
    bucket = os.environ["BUCKET_NAME"]
    s3 = _client("s3")
    uploads = []
    for key, size in zip(keys, sizes, strict=True):
        part_size = part_size_for(size, _env_int("PART_SIZE_BYTES", 64 * MiB))
        upload_id = s3.create_multipart_upload(
            Bucket=bucket, Key=key, Metadata=metadata, ContentType="application/gzip"
        )["UploadId"]
        parts = [
            {
                "part_number": n,
                "url": s3.generate_presigned_url(
                    "upload_part",
                    Params={"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": n},
                    ExpiresIn=expires,
                ),
            }
            for n in range(1, math.ceil(size / part_size) + 1)
        ]
        uploads.append({"key": key, "upload_id": upload_id, "part_size": part_size, "parts": parts})
    return 200, {"sample_id": sample_id, "uploads": uploads, "expires_in": expires}


def _upload_target(data: dict) -> tuple:
    key, upload_id = data.get("key"), data.get("upload_id")
    if not isinstance(key, str) or not isinstance(upload_id, str) or not upload_id:
        raise HttpError(400, "key and upload_id are required")
    # Only a key this API hands out: raw_reads/<sample>[ _R1| _R2].fastq.gz
    match = re.fullmatch(r"raw_reads/(.+?)(?:_(R1|R2))?" + re.escape(RAW_SUFFIX), key)
    if not match:
        raise HttpError(400, "key is not an upload key")
    _sample_id(match.group(1))
    return key, upload_id


def complete_upload(event: dict) -> tuple:
    _check_token(event["headers"])
    data = _json_body(event)
    key, upload_id = _upload_target(data)
    parts = data.get("parts")
    if (
        not isinstance(parts, list)
        or not parts
        or not all(
            isinstance(p, dict) and isinstance(p.get("part_number"), int) and isinstance(p.get("etag"), str)
            for p in parts
        )
    ):
        raise HttpError(400, "parts must list {part_number, etag} for every part")
    _client("s3").complete_multipart_upload(
        Bucket=os.environ["BUCKET_NAME"],
        Key=key,
        UploadId=upload_id,
        MultipartUpload={
            "Parts": [
                {"PartNumber": p["part_number"], "ETag": p["etag"]}
                for p in sorted(parts, key=lambda p: p["part_number"])
            ]
        },
    )
    return 200, {"key": key, "completed": True}


def abort_upload(event: dict) -> tuple:
    _check_token(event["headers"])
    key, upload_id = _upload_target(_json_body(event))
    _client("s3").abort_multipart_upload(Bucket=os.environ["BUCKET_NAME"], Key=key, UploadId=upload_id)
    return 200, {"key": key, "aborted": True}


def run_status(sample_id: str) -> tuple:
    sample_id = _sample_id(sample_id)
    sfn = _client("stepfunctions")
    kwargs = {"stateMachineArn": os.environ["STATE_MACHINE_ARN"], "maxResults": 100}
    for _ in range(STATUS_PAGES):
        page = sfn.list_executions(**kwargs)
        for run in page.get("executions", []):  # newest first
            name = run["name"]
            if name.startswith(sample_id) and RUN_NAME_TAIL.match(name[len(sample_id) :]):
                stopped = run.get("stopDate")
                return 200, {
                    "sample_id": sample_id,
                    "run_name": name,
                    "status": run["status"],
                    "started": run["startDate"].isoformat(),
                    "stopped": stopped.isoformat() if stopped else None,
                }
        if not page.get("nextToken"):
            break
        kwargs["nextToken"] = page["nextToken"]
    return 404, {"sample_id": sample_id, "status": "NOT_FOUND"}


def file_redirect(sample_id: str, kind: str) -> tuple:
    sample_id = _sample_id(sample_id)
    if kind not in FILE_KEY_TEMPLATES:
        raise HttpError(404, f"no such file kind: one of {sorted(FILE_KEY_TEMPLATES)}")
    key = FILE_KEY_TEMPLATES[kind].format(id=sample_id)
    bucket = os.environ["BUCKET_NAME"]
    s3 = _client("s3")
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception as err:  # noqa: BLE001
        code = str(getattr(err, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HttpError(404, f"{sample_id} has no {kind} yet") from None
        raise
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{key.rsplit("/", 1)[-1]}"',
        },
        ExpiresIn=_env_int("DOWNLOAD_URL_SECONDS", 900),
    )
    return 302, {"location": url}


ROUTES = {
    ("POST", "/uploads"): ("upload", start_upload),
    ("POST", "/uploads/complete"): ("complete", complete_upload),
    ("POST", "/uploads/abort"): ("abort", abort_upload),
}


def route_name(path: str) -> str:
    """The route a path names, for the log line (never the path itself: it carries the sample id)."""
    for (_, route_path), (name, _) in ROUTES.items():
        if path == route_path:
            return name
    parts = [p for p in path.split("/") if p]
    if len(parts) == 2 and parts[0] == "status":
        return "status"
    if len(parts) == 3 and parts[0] == "files":
        return "files"
    return "unknown"


def _route(method: str, path: str, event: dict) -> tuple:
    if (method, path) in ROUTES:
        return ROUTES[(method, path)][1](event)
    parts = [p for p in path.split("/") if p]
    name = route_name(path)
    if name in ("status", "files") and method != "GET":
        raise HttpError(405, "use GET")
    if name == "status":
        return run_status(parts[1])
    if name == "files":
        return file_redirect(parts[1], parts[2])
    if name != "unknown":
        raise HttpError(405, "use POST")
    raise HttpError(404, "no such route")


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "/")
    # Header names arrive lower-cased from a function URL; normalise anyway (tests, other front doors).
    event = {**event, "headers": {k.lower(): v for k, v in (event.get("headers") or {}).items()}}
    route = route_name(path)
    try:
        status, payload = _route(method, path, event)
    except HttpError as err:
        status, payload = err.status, {"error": err.message}
    except Exception as err:  # noqa: BLE001 - never a stack trace (or anything from the request) to the caller
        logger.error(f"{method} {route}: {type(err).__name__}")
        status, payload = 500, {"error": "internal error"}
    # Only these three things are logged: no headers (the token), no body, no presigned URL.
    logger.info(f"{method} {route} -> {status}")
    headers = {"content-type": "application/json", "cache-control": "no-store"}
    if status == 302:
        return {
            "statusCode": 302,
            "headers": {"location": payload["location"], "cache-control": "no-store"},
            "body": "",
        }
    return {"statusCode": status, "headers": headers, "body": json.dumps(payload)}
