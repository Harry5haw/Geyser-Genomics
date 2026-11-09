"""lambda/web_api/index.py: the hosted site's API. Fake S3, Step Functions and SSM clients: no AWS calls."""

import base64
import json
import logging
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from conftest import load_module

TOKEN = "t0k3n-" + "x" * 40


class FakeS3:
    def __init__(self, missing=()):
        self.calls = []
        self.missing = set(missing)

    def create_multipart_upload(self, **kw):
        self.calls.append(("create", kw))
        return {"UploadId": f"up-{kw['Key'].rsplit('/', 1)[-1]}"}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("presign", op, Params, ExpiresIn))
        return f"https://bucket.s3.eu-west-2.amazonaws.com/{Params['Key']}?op={op}&n={Params.get('PartNumber', '')}"

    def complete_multipart_upload(self, **kw):
        self.calls.append(("complete", kw))
        return {}

    def abort_multipart_upload(self, **kw):
        self.calls.append(("abort", kw))
        return {}

    def head_object(self, Bucket, Key):
        if Key in self.missing:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": 1}

    # Resume: the parts S3 holds per upload id, served two per page (to exercise the pagination).
    uploaded = {}

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker=0):
        self.calls.append(("list_parts", UploadId, PartNumberMarker))
        if UploadId not in self.uploaded:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchUpload", "Message": "gone"}}, "ListParts")
        parts = [p for p in self.uploaded[UploadId] if p["PartNumber"] > PartNumberMarker][:2]
        more = bool(parts) and parts[-1]["PartNumber"] < self.uploaded[UploadId][-1]["PartNumber"]
        page = {"Parts": parts, "IsTruncated": more}
        if more:
            page["NextPartNumberMarker"] = parts[-1]["PartNumber"]
        return page


class FakeSfn:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_executions(self, **kw):
        self.calls.append(kw)
        return self.pages[len(self.calls) - 1]


class FakeSsm:
    def __init__(self, value=TOKEN, fail=False):
        self.value, self.fail, self.calls = value, fail, 0

    def get_parameter(self, Name, WithDecryption):
        self.calls += 1
        assert Name == "/geyser/web/upload-token" and WithDecryption
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"Parameter": {"Value": self.value}}


@pytest.fixture
def api(monkeypatch):
    module = load_module("geyser_web_api", "lambda", "web_api", "index.py")
    monkeypatch.setenv("BUCKET_NAME", "lake")
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:eu-west-2:123:stateMachine:geyser")
    monkeypatch.setenv("UPLOAD_TOKEN_PARAMETER", "/geyser/web/upload-token")
    for var in ("MAX_UPLOAD_BYTES", "PART_SIZE_BYTES", "UPLOAD_URL_SECONDS", "DOWNLOAD_URL_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    module._clients.update({"s3": FakeS3(), "ssm": FakeSsm(), "stepfunctions": FakeSfn([{"executions": []}])})
    module._token = None
    return module


def call(api, method, path, body=None, token=TOKEN, b64=False):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    raw = json.dumps(body) if body is not None else ""
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": headers,
        "body": base64.b64encode(raw.encode()).decode() if b64 else raw,
        "isBase64Encoded": b64,
    }
    response = api.handler(event, None)
    payload = json.loads(response["body"]) if response["body"] else None
    return response["statusCode"], payload, response["headers"]


# --- uploads: the token ---------------------------------------------------------------------------------


def test_an_upload_needs_the_token(api):
    status, payload, _ = call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]}, token=None)
    assert status == 401 and "token" in payload["error"]
    assert call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]}, token="wrong")[0] == 401
    assert call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]}, token=TOKEN[:-1])[0] == 401
    assert not [c for c in api._clients["s3"].calls if c[0] == "create"]  # nothing started


def test_the_token_is_read_once_per_cold_start_and_never_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        assert call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]})[0] == 200
    call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]}, token="wrong")
    assert api._clients["ssm"].calls == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in logged and "Bearer" not in logged and "wrong" not in logged
    assert "POST upload -> 200" in logged and "POST upload -> 401" in logged


@pytest.mark.parametrize("ssm", [FakeSsm(fail=True), FakeSsm(value="short")])
def test_a_missing_or_short_token_parameter_disables_uploads_without_saying_why(api, ssm):
    api._clients["ssm"] = ssm
    status, payload, _ = call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]})
    assert status == 503 and payload == {"error": "uploads are unavailable"}


def test_without_a_token_parameter_name_uploads_are_off(api, monkeypatch):
    monkeypatch.delenv("UPLOAD_TOKEN_PARAMETER")
    assert call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]})[0] == 503


# --- uploads: presigned multipart ---------------------------------------------------------------------------


def test_a_single_end_upload_gets_a_presigned_multipart_upload_with_its_settings(api):
    MiB = 1024 * 1024
    body = {"sample_id": "S1", "sizes": [130 * MiB], "metadata": {"gate-profile": "smoke", "annotate": "alphagenome"}}
    status, payload, headers = call(api, "POST", "/uploads", body)
    assert status == 200 and headers["cache-control"] == "no-store"
    (upload,) = payload["uploads"]
    assert upload["key"] == "raw_reads/S1.fastq.gz" and upload["part_size"] == 64 * MiB
    assert [p["part_number"] for p in upload["parts"]] == [1, 2, 3]  # 130 MiB in 64 MiB parts
    s3 = api._clients["s3"]
    (create,) = [c[1] for c in s3.calls if c[0] == "create"]
    batch = create["Metadata"].pop("upload-batch")  # one random id per call (the trigger pairs mates by it)
    assert len(batch) == 16 and int(batch, 16) >= 0
    assert create == {
        "Bucket": "lake",
        "Key": "raw_reads/S1.fastq.gz",
        "Metadata": {"gate-profile": "smoke", "annotate": "alphagenome"},  # the trigger reads these
        "ContentType": "application/gzip",
    }
    presigns = [c for c in s3.calls if c[0] == "presign"]
    assert {c[1] for c in presigns} == {"upload_part"} and {c[3] for c in presigns} == {3600}
    assert [c[2]["PartNumber"] for c in presigns] == [1, 2, 3]
    assert all(c[2]["UploadId"] == "up-S1.fastq.gz" for c in presigns)


def test_a_paired_upload_gets_both_mates_r1_first(api):
    status, payload, _ = call(api, "POST", "/uploads", {"sample_id": "P1", "layout": "paired", "sizes": [10, 20]})
    assert status == 200
    assert [u["key"] for u in payload["uploads"]] == ["raw_reads/P1_R1.fastq.gz", "raw_reads/P1_R2.fastq.gz"]


def test_both_mates_of_one_call_share_one_batch_and_each_call_gets_a_new_one(api):
    # The trigger starts a paired run only on mates of one batch: never a new R1 with an earlier upload's R2.
    body = {"sample_id": "P1", "layout": "paired", "sizes": [10, 20]}
    call(api, "POST", "/uploads", body)
    call(api, "POST", "/uploads", body)
    batches = [c[1]["Metadata"]["upload-batch"] for c in api._clients["s3"].calls if c[0] == "create"]
    assert len(batches) == 4 and batches[0] == batches[1] and batches[2] == batches[3] and batches[0] != batches[2]


def test_the_caller_cannot_set_the_batch(api):
    body = {"sample_id": "P1", "layout": "paired", "sizes": [10, 20], "metadata": {"upload-batch": "old"}}
    assert call(api, "POST", "/uploads", body)[0] == 400


def test_the_batch_key_is_the_one_the_trigger_reads(api):
    trigger = load_module("trigger_for_batch", "lambda", "trigger", "index.py")
    assert api.UPLOAD_BATCH_METADATA == trigger.UPLOAD_BATCH_METADATA == "upload-batch"


def test_a_huge_file_gets_bigger_parts_to_stay_within_10000(api):
    size = 1024 * 1024 * 1024 * 1000  # 1000 GiB: 64 MiB parts would be 16,000
    api_size = api.part_size_for(size, 64 * 1024 * 1024)
    assert api_size % (1024 * 1024) == 0 and -(-size // api_size) <= 10_000
    assert api.part_size_for(1, 64 * 1024 * 1024) == 64 * 1024 * 1024


@pytest.mark.parametrize(
    "sample_id",
    ["", "S1_R1", "S1_R2", "../x", "a/b", "S1.x", "-S1", "x" * 31, 42, None, "S 1", "é1"],
)
def test_an_unsafe_sample_id_is_refused(api, sample_id):
    assert call(api, "POST", "/uploads", {"sample_id": sample_id, "sizes": [10]})[0] == 400


@pytest.mark.parametrize(
    "body",
    [
        {"sample_id": "S1"},  # no sizes
        {"sample_id": "S1", "sizes": [10, 20]},  # two sizes for one file
        {"sample_id": "S1", "layout": "paired", "sizes": [10]},
        {"sample_id": "S1", "sizes": [0]},
        {"sample_id": "S1", "sizes": [True]},
        {"sample_id": "S1", "sizes": ["10"]},
        {"sample_id": "S1", "sizes": [64 * 1024**3 + 1]},  # over MAX_UPLOAD_BYTES
        {"sample_id": "S1", "layout": "interleaved", "sizes": [10]},
        {"sample_id": "S1", "sizes": [10], "metadata": {"gate_profile": "smoke"}},  # not the trigger's spelling
        {"sample_id": "S1", "sizes": [10], "metadata": {"caller": "deepvariant"}},
        {"sample_id": "S1", "sizes": [10], "metadata": {"reference": 7}},
        {"sample_id": "S1", "sizes": [10], "metadata": {"reference": "x" * 101}},
        {"sample_id": "S1", "sizes": [10], "metadata": {"reference": "a\nb"}},
        {"sample_id": "S1", "sizes": [10], "metadata": ["smoke"]},
    ],
)
def test_a_malformed_upload_request_is_refused_before_anything_starts(api, body):
    assert call(api, "POST", "/uploads", body)[0] == 400
    assert not api._clients["s3"].calls


def test_a_base64_body_is_read(api):
    assert call(api, "POST", "/uploads", {"sample_id": "S1", "sizes": [10]}, b64=True)[0] == 200


@pytest.mark.parametrize("body", ["not json", "[1, 2]"])
def test_a_body_that_is_not_a_json_object_is_refused(api, body):
    event = {
        "rawPath": "/uploads",
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"authorization": f"Bearer {TOKEN}"},
        "body": body,
    }
    assert api.handler(event, None)["statusCode"] == 400


def test_completing_an_upload_sends_its_parts_in_order(api):
    body = {
        "key": "raw_reads/S1.fastq.gz",
        "upload_id": "up-1",
        "parts": [{"part_number": 2, "etag": '"b"'}, {"part_number": 1, "etag": '"a"'}],
    }
    assert call(api, "POST", "/uploads/complete", body)[0] == 200
    (complete,) = [c[1] for c in api._clients["s3"].calls if c[0] == "complete"]
    assert complete["Key"] == "raw_reads/S1.fastq.gz" and complete["UploadId"] == "up-1"
    assert complete["MultipartUpload"]["Parts"] == [{"PartNumber": 1, "ETag": '"a"'}, {"PartNumber": 2, "ETag": '"b"'}]


@pytest.mark.parametrize(
    "key", ["variants/S1.vcf.gz", "raw_reads/../x.fastq.gz", "raw_reads/S1.fq.gz", "raw_reads/sub/S1.fastq.gz"]
)
def test_only_upload_keys_can_be_completed_or_aborted(api, key):
    parts = [{"part_number": 1, "etag": "a"}]
    assert call(api, "POST", "/uploads/complete", {"key": key, "upload_id": "u", "parts": parts})[0] == 400
    assert call(api, "POST", "/uploads/abort", {"key": key, "upload_id": "u"})[0] == 400
    assert not api._clients["s3"].calls


def test_completing_and_aborting_need_the_token(api):
    body = {"key": "raw_reads/S1_R2.fastq.gz", "upload_id": "u", "parts": [{"part_number": 1, "etag": "a"}]}
    assert call(api, "POST", "/uploads/complete", body, token=None)[0] == 401
    assert call(api, "POST", "/uploads/abort", body, token="wrong")[0] == 401
    assert call(api, "POST", "/uploads/abort", body)[0] == 200
    assert [c[0] for c in api._clients["s3"].calls] == ["abort"]


# --- resume: POST /uploads/parts (T3's upload page, after a reload or past the URLs' hour) -------------------------


def _uploaded(api, upload_id, numbers):
    api._clients["s3"].uploaded = {
        upload_id: [{"PartNumber": n, "ETag": f'"e{n}"', "Size": 64 * 1024 * 1024} for n in numbers]
    }


def test_resume_lists_what_s3_has_and_presigns_what_is_asked_for(api):
    _uploaded(api, "up-1", [1, 2, 3, 5, 6])  # five parts, over three list_parts pages
    body = {"key": "raw_reads/S1_R1.fastq.gz", "upload_id": "up-1", "part_numbers": [4, 7]}
    status, payload, _ = call(api, "POST", "/uploads/parts", body)
    assert status == 200 and payload["key"] == "raw_reads/S1_R1.fastq.gz" and payload["expires_in"] == 3600
    assert [p["part_number"] for p in payload["done"]] == [1, 2, 3, 5, 6]  # S3 is the source of truth
    assert payload["done"][0] == {"part_number": 1, "etag": '"e1"', "size": 64 * 1024 * 1024}
    assert [p["part_number"] for p in payload["parts"]] == [4, 7]
    s3 = api._clients["s3"]
    assert [c[2] for c in s3.calls if c[0] == "list_parts"] == [0, 2, 5]  # paginated by PartNumberMarker
    presigns = [c for c in s3.calls if c[0] == "presign"]
    assert {c[1] for c in presigns} == {"upload_part"}
    assert [c[2]["PartNumber"] for c in presigns] == [4, 7] and all(c[2]["UploadId"] == "up-1" for c in presigns)


def test_resume_with_no_part_numbers_only_reports_progress(api):
    _uploaded(api, "up-1", [1])
    status, payload, _ = call(api, "POST", "/uploads/parts", {"key": "raw_reads/S1.fastq.gz", "upload_id": "up-1"})
    assert status == 200 and len(payload["done"]) == 1 and payload["parts"] == []


def test_resuming_an_unknown_or_finished_upload_is_a_404(api):
    _uploaded(api, "up-1", [1])
    body = {"key": "raw_reads/S1.fastq.gz", "upload_id": "gone", "part_numbers": [2]}
    status, payload, _ = call(api, "POST", "/uploads/parts", body)
    assert status == 404 and "start the upload again" in payload["error"]
    assert not [c for c in api._clients["s3"].calls if c[0] == "presign"]  # no URLs for a dead upload


@pytest.mark.parametrize("part_numbers", [[0], [10_001], [1, 1], ["2"], [True], 3, list(range(1, 1002))])
def test_resume_refuses_bad_part_numbers(api, part_numbers):
    body = {"key": "raw_reads/S1.fastq.gz", "upload_id": "up-1", "part_numbers": part_numbers}
    assert call(api, "POST", "/uploads/parts", body)[0] == 400
    assert not api._clients["s3"].calls


@pytest.mark.parametrize("key", ["variants/S1.vcf.gz", "raw_reads/../x.fastq.gz", "raw_reads/S1_R1.fq.gz"])
def test_resume_only_for_upload_keys(api, key):
    assert call(api, "POST", "/uploads/parts", {"key": key, "upload_id": "u", "part_numbers": [1]})[0] == 400


def test_resume_needs_the_token(api):
    body = {"key": "raw_reads/S1.fastq.gz", "upload_id": "up-1", "part_numbers": [1]}
    assert call(api, "POST", "/uploads/parts", body, token=None)[0] == 401
    assert call(api, "POST", "/uploads/parts", body, token="wrong")[0] == 401
    assert not api._clients["s3"].calls


# --- status ------------------------------------------------------------------------------------------------------


def _run(name, status="SUCCEEDED", stopped=True):
    start = datetime(2026, 11, 4, 1, 2, 3, tzinfo=UTC)
    return {"name": name, "status": status, "startDate": start, **({"stopDate": start} if stopped else {})}


def test_status_is_the_samples_latest_run_and_needs_no_token(api):
    api._clients["stepfunctions"] = FakeSfn(
        [
            {
                "executions": [
                    _run("S10-20261104T010203Z-aaaaaaaa"),  # another sample sharing the prefix
                    _run("S1-x-20261104T010203Z-bbbbbbbb"),  # likewise
                    _run("S1-20261104T010203Z-cccccccc", "RUNNING", stopped=False),
                    _run("S1-20261103T010203Z-dddddddd", "FAILED"),
                ]
            }
        ]
    )
    status, payload, _ = call(api, "GET", "/status/S1", token=None)
    assert status == 200
    assert payload == {
        "sample_id": "S1",
        "run_name": "S1-20261104T010203Z-cccccccc",
        "status": "RUNNING",
        "started": "2026-11-04T01:02:03+00:00",
        "stopped": None,
    }


def test_status_pages_back_a_few_pages_then_says_not_found(api):
    sfn = FakeSfn([{"executions": [_run("X-20261104T010203Z-aaaaaaaa")], "nextToken": "t"}] * 3)
    api._clients["stepfunctions"] = sfn
    status, payload, _ = call(api, "GET", "/status/S1", token=None)
    assert status == 404 and payload["status"] == "NOT_FOUND"
    assert len(sfn.calls) == api.STATUS_PAGES and sfn.calls[1]["nextToken"] == "t"
    assert sfn.calls[0]["stateMachineArn"].endswith(":stateMachine:geyser")


def test_status_refuses_an_unsafe_id(api):
    assert call(api, "GET", "/status/a.b", token=None)[0] == 400


ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$")


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 11, 4, 1, 2, 3, tzinfo=UTC),  # what boto3 returns
        datetime(2026, 11, 4, 2, 2, 3, tzinfo=timezone(timedelta(hours=1))),  # another offset: converted to UTC
        datetime(2026, 11, 4, 1, 2, 3),  # naive: taken as UTC, never sent naive
    ],
)
def test_status_times_are_iso_8601_utc_with_an_explicit_offset(api, start):
    # The published pages' reload guard (W2, #233) parses `stopped`: never epoch seconds, never a naive time.
    run = {"name": "S1-20261104T010203Z-cccccccc", "status": "SUCCEEDED", "startDate": start, "stopDate": start}
    api._clients["stepfunctions"] = FakeSfn([{"executions": [run]}])
    _, payload, _ = call(api, "GET", "/status/S1", token=None)
    for field in ("started", "stopped"):
        assert isinstance(payload[field], str) and ISO_UTC.match(payload[field]), payload[field]
        assert datetime.fromisoformat(payload[field]) == datetime(2026, 11, 4, 1, 2, 3, tzinfo=UTC)


# --- files -------------------------------------------------------------------------------------------------------


def test_a_file_is_a_redirect_to_a_short_lived_presigned_get(api):
    status, payload, headers = call(api, "GET", "/files/S1/vcf", token=None)
    assert status == 302 and payload is None
    assert headers["location"].startswith("https://bucket.s3.eu-west-2.amazonaws.com/variants/S1.vcf.gz")
    (presign,) = [c for c in api._clients["s3"].calls if c[0] == "presign"]
    assert presign[1] == "get_object" and presign[3] == 900
    assert presign[2]["ResponseContentDisposition"] == 'attachment; filename="S1.vcf.gz"'
    assert "ResponseContentType" not in presign[2]


def test_the_fastp_report_opens_in_the_browser_as_html(api):
    # The site's "View the full fastp report" link: inline, as text/html (T3's W2 pages), not a download.
    status, _, headers = call(api, "GET", "/files/S1/qc_html", token=None)
    assert status == 302 and "qc_reports/S1.fastp.html" in headers["location"]
    (presign,) = [c for c in api._clients["s3"].calls if c[0] == "presign"]
    assert presign[2]["ResponseContentDisposition"] == 'inline; filename="S1.fastp.html"'
    assert presign[2]["ResponseContentType"] == "text/html; charset=utf-8"
    assert set(api.INLINE_FILES) <= set(api.PUBLIC_FILES)  # never a token-only kind


def test_the_public_kinds_are_the_ones_the_published_pages_link_to(api):
    # T3's W2 download hrefs, {API}/files/<id>/<kind>: exactly the public allowlist.
    pages = ["vcf", "tbi", "csq_vcf", "csq_tbi", "qc_html", "qc_json", "gate_verdict"]
    pages += ["flagstat", "mosdepth_summary", "mosdepth_dist", "bcftools_stats", "run_record"]
    # The calls of a DeepVariant- or pangenome-called sample (the page picks by the record's VCF folder).
    pages += ["deepvariant_vcf", "deepvariant_tbi", "pangenome_vcf", "pangenome_tbi"]
    assert sorted(api.PUBLIC_FILES) == sorted(pages)
    assert api.PUBLIC_FILES["deepvariant_vcf"].format(id="S1") == "variants_deepvariant/S1.vcf.gz"
    assert api.PUBLIC_FILES["pangenome_vcf"].format(id="S1") == "variants_pangenome/S1.vcf.gz"


PUBLIC_KINDS = [
    "vcf",
    "tbi",
    "csq_vcf",
    "csq_tbi",
    "qc_html",
    "qc_json",
    "gate_verdict",
    "flagstat",
    "mosdepth_summary",
    "mosdepth_dist",
    "bcftools_stats",
    "run_record",
    "deepvariant_vcf",
    "deepvariant_tbi",
    "pangenome_vcf",
    "pangenome_tbi",
]


@pytest.mark.parametrize("kind", PUBLIC_KINDS)
def test_the_calls_qc_stats_and_run_record_are_public(api, kind):
    status, _, headers = call(api, "GET", f"/files/S1/{kind}", token=None)
    assert status == 302 and headers["location"].startswith("https://bucket.s3.eu-west-2.amazonaws.com/")
    assert sorted(api.PUBLIC_FILES) == sorted(PUBLIC_KINDS)  # the allowlist is exactly the user's choice


@pytest.mark.parametrize("kind", ["cram", "crai", "bam"])
@pytest.mark.parametrize("token", [None, "wrong", TOKEN[:-1]])
def test_alignments_without_the_token_are_refused_before_any_lookup(api, kind, token):
    status, payload, _ = call(api, "GET", f"/files/S1/{kind}", token=token)
    assert status == 401 and "token" in payload["error"]
    assert not api._clients["s3"].calls  # not even a HeadObject: nothing about the file is revealed


@pytest.mark.parametrize("kind", ["cram", "crai", "bam"])
def test_alignments_with_the_token_come_as_a_url_the_page_follows(api, kind):
    status, payload, headers = call(api, "GET", f"/files/S1/{kind}")
    assert status == 200 and headers["cache-control"] == "no-store"
    assert payload["url"].startswith("https://bucket.s3.eu-west-2.amazonaws.com/alignments/S1.")
    assert payload["expires_in"] == 900


@pytest.mark.parametrize("kind", ["raw", "raw_R1", "raw_R2", "reads", "fastq", "provenance", "..", "VCF", "CRAM"])
@pytest.mark.parametrize("token", [None, TOKEN])
def test_raw_reads_and_anything_unlisted_are_never_offered(api, kind, token):
    assert call(api, "GET", f"/files/S1/{kind}", token=token)[0] == 404
    assert not api._clients["s3"].calls


def test_a_file_not_written_yet_is_a_404(api):
    api._clients["s3"] = FakeS3(missing={"qc_reports/S1.gate.json"})
    status, payload, _ = call(api, "GET", "/files/S1/gate_verdict", token=None)
    assert status == 404 and "no gate_verdict yet" in payload["error"]


# --- routing ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,expected",
    [("GET", "/", 404), ("GET", "/uploads", 405), ("POST", "/status/S1", 405), ("DELETE", "/files/S1/vcf", 405)],
)
def test_unknown_routes_and_methods(api, method, path, expected):
    assert call(api, method, path)[0] == expected


def test_an_unexpected_error_is_a_bare_500(api, caplog):
    class Broken(FakeSfn):
        def list_executions(self, **kw):
            raise RuntimeError("secret detail")

    api._clients["stepfunctions"] = Broken([])
    status, payload, _ = call(api, "GET", "/status/S1", token=None)
    assert status == 500 and payload == {"error": "internal error"}
    assert "secret detail" not in "\n".join(r.getMessage() for r in caplog.records)


# --- consistency: the Lambda zip can't import app/ or the trigger, so their conventions live here too ----------


def test_file_kinds_are_the_stages_own_keys(api):
    # Every output tasks.keys_for names is either public or token-only, never both, and the raw reads neither.
    import provenance
    import tasks

    assert not set(api.PUBLIC_FILES) & set(api.TOKEN_FILES)
    offered = {k: t.format(id="S1") for k, t in {**api.PUBLIC_FILES, **api.TOKEN_FILES}.items()}
    # AlphaGenome's outputs (3.0) are not offered: the hosted page shows the table with the terms notice, no downloads.
    outputs = {k: v for k, v in tasks.keys_for("S1").items() if not k.startswith(("raw", "alphagenome"))}
    # The extra callers' VCFs (and their indexes) are provenance.STAGE_VCF's, the record's "vcf" when they called.
    callers = {
        "deepvariant_vcf": provenance.STAGE_VCF["DeepVariant"].format(id="S1"),
        "pangenome_vcf": provenance.STAGE_VCF["PangenomeDeepVariant"].format(id="S1"),
    }
    callers |= {k.replace("_vcf", "_tbi"): v + ".tbi" for k, v in list(callers.items())}
    assert offered == {**outputs, "run_record": provenance.record_key("S1"), **callers}
    assert not any(key.startswith("raw_reads/") for key in offered.values())


def test_the_api_role_can_read_exactly_the_offered_prefixes(api):
    # web_api.tf grants s3:GetObject on these prefixes and on the run record only.
    import re

    from conftest import REPO_ROOT

    with open(f"{REPO_ROOT}/infrastructure/web_api.tf", encoding="utf-8") as fh:
        tf = fh.read()
    prefixes = re.search(r"web_download_prefixes\s*=\s*\[([^\]]*)\]", tf).group(1)
    offered = {t.split("/")[0] for t in {**api.PUBLIC_FILES, **api.TOKEN_FILES}.values()} - {"runs"}
    assert sorted(re.findall(r'"([\w-]+)"', prefixes)) == sorted(offered)
    assert offered == {"alignments", "qc_reports", "stats", "variants", "variants_deepvariant", "variants_pangenome"}
    assert "/runs/*.provenance.json" in tf
    # raw_reads/ appears once: the upload statement (PutObject), never in a GetObject grant.
    assert tf.count("/raw_reads/*") == 1
    assert tf.index("UploadRawReads") < tf.index("/raw_reads/*") < tf.index("DownloadOutputs")


@pytest.mark.parametrize("layout", ["single", "paired"])
def test_upload_keys_are_the_raw_keys_tasks_reads(api, layout):
    import tasks

    raw = [v for k, v in tasks.keys_for("S1", layout).items() if k.startswith("raw")]
    assert api.raw_keys("S1", layout) == raw
    assert api.MATES == tasks.MATES


def test_upload_metadata_uses_the_keys_the_trigger_reads(api, trigger):
    known = set(trigger.GATE_PROFILE_METADATA) | set(trigger.REFERENCE_METADATA)
    known |= set(getattr(trigger, "ANNOTATE_METADATA", ("annotate",)))  # annotate arrives with #209
    assert set(api.METADATA_KEYS) <= known


def test_sample_ids_survive_the_execution_name_unchanged(api, trigger):
    # The status lookup matches the execution name's first part to the id: the trigger must not rewrite it.
    for sample_id in ("S1", "SRR062634", "my-sample_2", "x" * 30):
        assert trigger.build_execution_name(sample_id, "2026-11-04T01:02:03Z", "k").startswith(f"{sample_id}-")
        assert api.SAMPLE_ID.match(sample_id)
