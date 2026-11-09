"""v1.3 "Big inputs" (docs/design/genome-scale-app.md): storage ETags and metadata, preflight, align_chunk,
align_merge and region-streamed calling, offline with faked tools. The Docker E2E checks the real tools."""

import gzip
import json
import shlex
import subprocess

import pytest

import provenance
import tasks
from conftest import stats_fixture

REF = "ref.fa"
GAPPED_REF = b">c\n" + b"N" * tasks.BWA_MEM2_SAFE_N_PREFIX + b"\nACGT\n"
RUN = "S1-20261110T090000Z-1a2b3c4d"


def put(root, key, data=b"x"):
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fastq(n, mate=None, start=0):
    suffix = f"/{mate}" if mate else ""
    return "".join(f"@read{i}{suffix} extra\nACGTACGT\n+\nIIIIIIII\n" for i in range(start, start + n)).encode()


def records(data: bytes) -> list:
    lines = data.decode().splitlines()
    return ["\n".join(lines[i : i + 4]) for i in range(0, len(lines), 4)]


@pytest.fixture
def run_name(monkeypatch):
    monkeypatch.setenv("RUN_NAME", RUN)
    monkeypatch.setattr(provenance, "_current", None)
    return RUN


# --- storage: ETags, metadata, in-place URLs -----------------------------------------------------------------


def test_local_storage_heads_and_keeps_metadata(local_env, tmp_path):
    store = tasks.storage()
    src = tmp_path / "f.bin"
    src.write_bytes(b"hello")
    store.upload(str(src), "a/f.bin", metadata={"Cram-ETag": "abc"})
    head = store.head("a/f.bin")
    assert head == {"etag": "5d41402abc4b2a76b9719d911017c592", "size": 5, "metadata": {"cram-etag": "abc"}}
    assert list(store.list_keys("a/")) == ["a/f.bin"]  # the metadata sidecar is never listed
    store.upload(str(src), "a/f.bin")  # a new object without metadata has none, as in S3
    assert store.head("a/f.bin")["metadata"] == {}
    assert store.presigned_url("a/f.bin") == str(local_env / "a" / "f.bin")
    with pytest.raises(tasks.ObjectNotFound):
        store.head("a/missing")


# --- preflight -----------------------------------------------------------------------------------------------


def test_round_robin_split_keeps_every_read_and_equal_chunks(local_env, tmp_path, run_name):
    import io

    data = fastq(10)
    out = tasks.split_reads([io.BytesIO(gzip.compress(data))], ["r"], 3, str(tmp_path), tasks.SINGLE, "S1")
    assert out["reads"] == 10 and [c["reads"] for c in out["chunks"]] == [4, 3, 3]
    got = []
    for chunk in out["chunks"]:
        got += records(gzip.decompress(open(chunk["paths"]["raw"], "rb").read()))
    assert sorted(got) == sorted(records(data))


def test_paired_mates_stay_together(local_env, tmp_path, run_name):
    import io

    r1, r2 = fastq(7, 1), fastq(7, 2)
    out = tasks.split_reads(
        [io.BytesIO(gzip.compress(r1)), io.BytesIO(gzip.compress(r2))],
        ["R1", "R2"],
        2,
        str(tmp_path),
        tasks.PAIRED,
        "S1",
    )
    for chunk in out["chunks"]:
        names1 = [r.split()[0][:-2] for r in records(gzip.decompress(open(chunk["paths"]["raw_R1"], "rb").read()))]
        names2 = [r.split()[0][:-2] for r in records(gzip.decompress(open(chunk["paths"]["raw_R2"], "rb").read()))]
        assert names1 == names2 and len(names1) == chunk["reads"]


@pytest.mark.parametrize(
    "streams,error",
    [
        ([gzip.compress(fastq(5))[:-12]], "truncated or corrupt"),
        ([b"not gzip at all"], "truncated or corrupt"),
        ([gzip.compress(b"")], "no reads"),
        ([gzip.compress(fastq(5, 1)), gzip.compress(fastq(4, 2))], "different read counts"),
        ([gzip.compress(fastq(3, 1)), gzip.compress(fastq(3, 2, start=1))], "mates out of step"),
        ([gzip.compress(b"@r1\nACGT\n+\nIII\n")], "different lengths"),
        ([gzip.compress(b"r1\nACGT\n+\nIIII\n")], "not a FASTQ record"),
    ],
)
def test_preflight_catches_bad_inputs(tmp_path, streams, error):
    import io

    layout = tasks.SINGLE if len(streams) == 1 else tasks.PAIRED
    labels = ["r"] if layout == tasks.SINGLE else ["R1", "R2"]
    with pytest.raises(tasks.PreflightFailed, match=error):
        tasks.split_reads([io.BytesIO(s) for s in streams], labels, 2, str(tmp_path), layout, "S1")


def test_preflight_writes_chunks_manifest_and_report(local_env, run_name, capsys):
    put(local_env, "raw_reads/S1_R1.fastq.gz", gzip.compress(fastq(9, 1)))
    put(local_env, "raw_reads/S1_R2.fastq.gz", gzip.compress(fastq(9, 2)))
    assert tasks.main(["preflight", "S1", "--chunks", "4"]) == 0
    store = tasks.storage()
    manifest = json.loads((local_env / tasks.manifest_key("S1")).read_text())
    assert tasks.manifest_key("S1") == f"intermediate/chunks/S1/{RUN}/manifest.json"
    assert manifest["schema"] == 1 and manifest["layout"] == "paired" and manifest["reads"] == 9
    assert manifest["chunk_count"] == 4 and [c["reads"] for c in manifest["chunks"]] == [3, 2, 2, 2]
    first = manifest["chunks"][0]
    assert first["name"] == "chunk-000" and first["index"] == 0
    assert first["keys"] == {
        "raw_R1": f"intermediate/chunks/S1/{RUN}/chunk-000_R1.fastq.gz",
        "raw_R2": f"intermediate/chunks/S1/{RUN}/chunk-000_R2.fastq.gz",
    }
    assert first["aligned"] == f"intermediate/aligned/S1/{RUN}/chunk-000.bam"
    assert first["bytes"] == sum(store.head(k)["size"] for k in first["keys"].values())
    assert manifest["input"]["keys"] == ["raw_reads/S1_R1.fastq.gz", "raw_reads/S1_R2.fastq.gz"]
    report = json.loads((local_env / "qc_reports" / "S1.preflight.json").read_text())
    assert (
        report["ok"]
        and report["error"] is None
        and report["reads"] == 9
        and report["manifest"] == tasks.manifest_key("S1")
    )
    part = provenance.read_part(store, "S1", "Preflight")
    assert part["status"] == "Success" and part["preflight"]["chunks"] == 4
    assert "Pre-flight OK: 9 reads (paired) in 4 chunks" in capsys.readouterr().out


def test_a_failed_preflight_reports_and_exits_1_without_a_manifest(local_env, run_name, capsys):
    put(local_env, "raw_reads/S1.fastq.gz", gzip.compress(fastq(5))[:-12])
    assert tasks.main(["preflight", "S1", "--chunks", "2"]) == 1
    assert "PreflightFailed: the gzip stream is truncated or corrupt" in capsys.readouterr().out
    report = json.loads((local_env / "qc_reports" / "S1.preflight.json").read_text())
    assert report["ok"] is False and "truncated" in report["error"] and report["manifest"] is None
    assert not (local_env / tasks.manifest_key("S1")).exists()
    assert provenance.read_part(tasks.storage(), "S1", "Preflight")["status"] == "Failure"


def test_the_manifest_key_can_be_chosen(local_env, run_name):
    put(local_env, "raw_reads/S1.fastq.gz", gzip.compress(fastq(2)))
    assert tasks.main(["preflight", "S1", "--chunks", "1", "--manifest-key", "intermediate/x/m.json"]) == 0
    assert json.loads((local_env / "intermediate" / "x" / "m.json").read_text())["chunk_count"] == 1


@pytest.mark.parametrize(
    "argv", [["preflight", "S1", "--chunks", "0"], ["preflight", "S1", "--chunks", "1000"], ["align_chunk", "S1", REF]]
)
def test_usage_errors_exit_2(local_env, argv):
    with pytest.raises(SystemExit) as err:
        tasks.main(argv)
    assert err.value.code == 2


# --- align_chunk and align_merge ------------------------------------------------------------------------------


def preflighted(local_env, layout=tasks.PAIRED, n=2):
    if layout == tasks.PAIRED:
        put(local_env, "raw_reads/S1_R1.fastq.gz", gzip.compress(fastq(5, 1)))
        put(local_env, "raw_reads/S1_R2.fastq.gz", gzip.compress(fastq(5, 2)))
    else:
        put(local_env, "raw_reads/S1.fastq.gz", gzip.compress(fastq(5)))
    put(local_env, f"reference/{REF}", GAPPED_REF)
    tasks.preflight_task("S1", n)
    return json.loads((local_env / tasks.manifest_key("S1")).read_text())


def test_chunk_command_has_no_markdup(tmp_path):
    paired = tasks.chunk_align_command("r.fa", ["a_R1.fq.gz", "a_R2.fq.gz"], "o.bam", 8, "/w", "S1", "bwa-mem2")
    stages = paired.split(" | ")
    assert stages[0].startswith("bwa-mem2 mem -t 8 -R '@RG\\tID:S1\\tSM:S1' r.fa a_R1.fq.gz a_R2.fq.gz")
    assert stages[1] == "samtools fixmate -m -u - -" and stages[2].startswith("samtools sort -l 1")
    assert "markdup" not in paired and shlex.split(stages[2])[-3:] == ["-o", "o.bam", "-"]
    single = tasks.chunk_align_command("r.fa", ["a.fq.gz"], "o.bam", 2, "/w", "S1", "bwa")
    assert "fixmate" not in single and single.startswith("bwa mem")
    with pytest.raises(ValueError, match="giraffe"):
        tasks.chunk_align_command("r.fa", ["a.fq.gz"], "o.bam", 2, "/w", "S1", "giraffe")


def test_align_chunk_aligns_its_chunk(local_env, tool_calls, shell_commands, uploads, run_name):
    manifest = preflighted(local_env)
    shell_commands.clear()
    assert tasks.main(["align_chunk", "S1", REF, "--chunk", "1"]) == 0
    (align,) = shell_commands
    assert "chunk-001_R1.fastq.gz" in align and "chunk-001_R2.fastq.gz" in align and "markdup" not in align
    assert uploads[-1] == manifest["chunks"][1]["aligned"]
    assert not (local_env / "runs" / "S1.provenance" / "AlignChunk.json").exists()  # no part per chunk
    metrics = [json.loads(m) for m in (local_env / "metrics" / "metrics.jsonl").read_text().splitlines()]
    assert metrics[-1]["task"] == "AlignChunk" and metrics[-1]["status"] == "Success"


def test_align_chunk_rejects_a_chunk_not_in_the_manifest(local_env, tool_calls, shell_commands, run_name):
    preflighted(local_env)
    with pytest.raises(ValueError, match="chunk 7 is not in the manifest"):
        tasks.align_chunk_task("S1", REF, 7)


def test_align_chunk_without_a_manifest_names_it(local_env, run_name):
    with pytest.raises(FileNotFoundError, match="run preflight first"):
        tasks.align_chunk_task("S1", REF, 0)


def test_align_merge_writes_aligns_outputs_and_part(local_env, tool_calls, shell_commands, uploads, run_name):
    manifest = preflighted(local_env)
    for c in manifest["chunks"]:
        put(local_env, c["aligned"], b"BAM")
    shell_commands.clear()
    assert tasks.main(["align_merge", "S1", REF, "--threads", "3"]) == 0
    merge, index, flagstat, mosdepth = shell_commands
    words = shlex.split(merge.split(" | ")[0])
    assert words[:5] == ["samtools", "merge", "-@", "3", "-u"] and words[-1] == "-"  # to stdout
    assert "samtools markdup -s -@ 3" in merge and "-O cram" in merge
    assert uploads[-3:] == ["alignments/S1.cram.crai", "alignments/S1.cram", "alignments/S1.cram.crai"]
    store = tasks.storage()
    assert store.head("alignments/S1.cram.crai")["metadata"]["cram-etag"] == store.head("alignments/S1.cram")["etag"]
    part = provenance.read_part(store, "S1", "Align")
    assert part["status"] == "Success" and part["chunks"] == 2 and part["layout"] == "paired"
    assert {i["key"] for i in part["inputs"]} >= {c["aligned"] for c in manifest["chunks"]}
    assert (local_env / "stats" / "S1" / "flagstat.txt").read_bytes() == stats_fixture("single", "flagstat.txt")


def test_align_merge_reads_the_chunks_in_place(local_env, tool_calls, shell_commands, run_name, monkeypatch):
    manifest = preflighted(local_env)
    for c in manifest["chunks"]:
        put(local_env, c["aligned"], b"BAM")
    seen = {}
    real = tasks.run_shell

    def capture(command):
        if command.startswith("samtools merge"):
            words = shlex.split(command.split(" | ")[0])
            seen["inputs"] = open(words[words.index("-b") + 1]).read().split()
        real(command)

    monkeypatch.setattr(tasks, "run_shell", capture)
    tasks.align_merge_task("S1", REF)
    assert seen["inputs"] == [str(local_env.joinpath(*c["aligned"].split("/"))) for c in manifest["chunks"]]


def test_align_merge_names_missing_chunks_before_running(local_env, tool_calls, shell_commands, run_name):
    manifest = preflighted(local_env)
    put(local_env, manifest["chunks"][0]["aligned"], b"BAM")
    shell_commands.clear()
    with pytest.raises(FileNotFoundError, match="1 chunk BAM\\(s\\) missing.*chunk-001.bam"):
        tasks.align_merge_task("S1", REF)
    assert not shell_commands


def test_chunks_of_another_run_are_never_read(local_env, tool_calls, shell_commands, run_name, monkeypatch):
    preflighted(local_env)
    monkeypatch.setenv("RUN_NAME", "S1-other-run")
    with pytest.raises(FileNotFoundError, match="S1-other-run.manifest.json: run preflight first"):
        tasks.align_merge_task("S1", REF)


# --- region-streamed calling -----------------------------------------------------------------------------------


def aligned(local_env, etag_ok=True):
    """S1's CRAM and .crai in storage, the .crai recording the CRAM's ETag (or a stale one)."""
    put(local_env, f"reference/{REF}", GAPPED_REF)
    put(local_env, "alignments/S1.cram", b"CRAM")
    store = tasks.storage()
    path = local_env / "crai.tmp"
    path.write_bytes(b"CRAI")
    etag = store.head("alignments/S1.cram")["etag"] if etag_ok else "stale"
    store.upload(str(path), "alignments/S1.cram.crai", metadata={"cram-etag": etag})


def test_a_verified_index_lets_a_shard_read_its_region_in_place(local_env, tool_calls, shell_commands, capsys):
    aligned(local_env)
    tasks.variants_shard_task("S1", REF, "c:1-500")
    (call,) = shell_commands  # no samtools index, no CRAM download
    source = shlex.split(call)[shlex.split(call).index("c:1-500") + 1]
    location, crai = source.split("##idx##")
    assert location == str(local_env / "alignments" / "S1.cram") and crai.endswith("S1.cram.crai")
    assert "Read the region in place" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["stale", "no_metadata", "off"])
def test_a_shard_downloads_when_the_index_isnt_verified(local_env, tool_calls, shell_commands, monkeypatch, case):
    if case == "no_metadata":
        put(local_env, f"reference/{REF}", GAPPED_REF)
        put(local_env, "alignments/S1.cram", b"CRAM")
        put(local_env, "alignments/S1.cram.crai", b"CRAI")
    else:
        aligned(local_env, etag_ok=case != "stale")
    if case == "off":
        monkeypatch.setenv(tasks.STREAM_REGIONS_ENV, "0")
    tasks.variants_shard_task("S1", REF, "c:1-500")
    index, call = shell_commands
    assert shlex.split(index)[:2] == ["samtools", "index"] and "##idx##" not in call


def failing_in_place(monkeypatch, failures):
    """run_shell that fails the first `failures` in-place reads; returns the list of in-place attempts."""
    real, attempts = tasks.run_shell, []
    monkeypatch.setattr(tasks, "IN_PLACE_RETRY_SECONDS", 0)

    def flaky(command):
        if "##idx##" in command:
            attempts.append(command)
            if len(attempts) <= failures:
                raise subprocess.CalledProcessError(1, command)
        real(command)

    monkeypatch.setattr(tasks, "run_shell", flaky)
    return attempts


def test_a_failed_in_place_read_falls_back_to_downloading(local_env, tool_calls, shell_commands, monkeypatch, capsys):
    aligned(local_env)
    attempts = failing_in_place(monkeypatch, failures=99)
    tasks.variants_shard_task("S1", REF, "c:1-500")
    assert len(attempts) == tasks.IN_PLACE_ATTEMPTS  # retried before giving up
    index, call = shell_commands
    assert shlex.split(index)[:2] == ["samtools", "index"] and "##idx##" not in call
    assert f"attempt {tasks.IN_PLACE_ATTEMPTS}/{tasks.IN_PLACE_ATTEMPTS}); downloading" in capsys.readouterr().out


def test_one_transient_in_place_failure_is_retried_not_downloaded(local_env, tool_calls, shell_commands, monkeypatch):
    aligned(local_env)
    attempts = failing_in_place(monkeypatch, failures=1)
    tasks.variants_shard_task("S1", REF, "c:1-500")
    assert len(attempts) == 2
    (call,) = shell_commands  # the retry's call only: no samtools index, no download
    assert "##idx##" in call


def test_a_download_that_cannot_fit_fails_before_it_starts(local_env, tool_calls, shell_commands, monkeypatch):
    aligned(local_env)
    failing_in_place(monkeypatch, failures=99)
    downloads = []
    monkeypatch.setattr(tasks, "fetch_alignment", lambda *a, **k: downloads.append(a) or "never")
    free = tasks.DOWNLOAD_HEADROOM_BYTES - 1  # less than the headroom alone
    monkeypatch.setattr(tasks.shutil, "disk_usage", lambda path: shutil_usage(free))
    with pytest.raises(tasks.DiskSpaceError, match=r"alignments/S1.cram .*for region c:1-500.*job_disk_gb"):
        tasks.variants_shard_task("S1", REF, "c:1-500")
    assert not downloads


def test_room_check_passes_with_enough_disk_or_an_unknown_size(local_env, monkeypatch, tmp_path):
    aligned(local_env)
    monkeypatch.setattr(tasks.shutil, "disk_usage", lambda path: shutil_usage(10 * tasks.DOWNLOAD_HEADROOM_BYTES))
    tasks.check_room_to_download("alignments/S1.cram", str(tmp_path), "for a test")
    monkeypatch.setattr(tasks.storage(), "head", lambda key: {"etag": "x", "size": None, "metadata": {}})
    monkeypatch.setattr(tasks.shutil, "disk_usage", lambda path: shutil_usage(0))
    tasks.check_room_to_download("alignments/S1.cram", str(tmp_path), "for a test")


def shutil_usage(free):
    import collections

    return collections.namedtuple("usage", "total used free")(free * 2, free, free)


def test_an_index_replaced_while_it_was_read_is_not_trusted(local_env, monkeypatch, tmp_path):
    aligned(local_env)
    store = tasks.storage()
    real = store.download

    def replace_after(key, path):
        real(key, path)
        other = tmp_path / "new.crai"
        other.write_bytes(b"NEW")
        store.upload(str(other), key)  # a re-run's new index, without the metadata yet

    monkeypatch.setattr(store, "download", replace_after)
    assert tasks.streamable_alignment("S1", str(tmp_path)) is None


# --- names the state machine uses -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job,stage",
    [("Preflight-S1-x", "Preflight"), ("AlignChunk-3-S1-x", "AlignChunk"), ("AlignMerge-S1-x", "Align")],
)
def test_job_names_map_to_stages(job, stage):
    assert provenance.parse_failure("States.TaskFailed", json.dumps({"JobName": job}))["stage"] == stage


def test_the_large_route_stages_are_tasks():
    assert {k: tasks.TASKS[k][1] for k in ("preflight", "align_chunk", "align_merge")} == {
        "preflight": False,
        "align_chunk": True,
        "align_merge": True,
    }


# --- run_local's large route ------------------------------------------------------------------------------------


def test_run_local_runs_the_large_route_instead_of_align():
    import run_local

    jobs = run_local.pipeline_jobs("S1", REF, chunks=3, gate=True)
    names = [j["name"] for j in jobs]
    assert names == ["qc", "preflight", "align_chunk", "align_chunk", "align_chunk", "align_merge", "variants", "gate"]
    assert jobs[1]["cmd"] == ["python", "tasks.py", "preflight", "S1", "--chunks", "3"]
    assert [j["cmd"][-1] for j in jobs if j["name"] == "align_chunk"] == ["0", "1", "2"]
    assert jobs[5]["cmd"] == ["python", "tasks.py", "align_merge", "S1", REF]
    assert "align" not in names and [j["name"] for j in run_local.pipeline_jobs("S1", REF)][:2] == ["qc", "align"]


@pytest.mark.parametrize("argv", [["--chunks", "0"], ["--chunks", "1000"], ["--chunks", "2", "--skip-to-variants"]])
def test_run_local_rejects_bad_chunk_options(argv, tmp_path):
    import run_local

    with pytest.raises(SystemExit) as err:
        run_local.main(["--root", str(tmp_path), *argv])
    assert err.value.code == 2


def test_run_local_passes_the_streaming_switch():
    import run_local

    assert "GEYSER_STREAM_REGIONS" in run_local.PIPELINE_ENV_VARS


def test_run_local_caps_memory_and_labels_every_container(monkeypatch, tmp_path):
    import run_local

    ran = []
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    job = run_local.pipeline_jobs("S1", REF)[1]  # align
    assert run_local.run_job(job, "docker", str(tmp_path), memory="8g", label="geyser.owner=t2") == 0
    (cmd,) = ran
    assert cmd[cmd.index("--memory") + 1] == "8g" and cmd[cmd.index("--memory-swap") + 1] == "8g"
    assert cmd[cmd.index("--label") + 1] == "geyser.owner=t2"
