"""DeepVariant split mode (v1.5): make_examples parts on CPU jobs, call_variants + postprocess_variants on the GPU job.

The wrapper, samtools and the GPU are faked; the fakes write the files DeepVariant 1.10.0 really writes per task
(seen on the synthetic E2E, 4 Nov 2026) and the dry-run plan is the real one (tests/fixtures/deepvariant).
"""

import gzip
import json
import os
import re

import pytest

import deepvariant_task as dv
import run_local

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "deepvariant", "dry-run-4-shards.txt")
RUN = "S1-20261104T000000Z-test"


def real_plan():
    with open(FIXTURE, encoding="utf-8") as fh:
        return dv.step_commands([(0.0, line.rstrip("\n")) for line in fh])


def option(cmd, name):
    return next(c.split("=", 1)[1] for c in cmd if c.startswith(f"--{name}="))


def plan_for(cmd):
    """The real dry-run plan with this command's paths and task count (what --dry_run=true prints for it)."""
    n = int(option(cmd, "num_shards"))
    swaps = {
        "/w/ref.fa": option(cmd, "ref"),
        "/w/S1.cram": option(cmd, "reads"),
        "/w/i": option(cmd, "intermediate_results_dir"),
        "/w/out.vcf.gz": option(cmd, "output_vcf"),
        "@4.gz": f"@{n}.gz",
        "seq 0 3": f"seq 0 {n - 1}",
        '--cpus "4"': f'--cpus "{n}"',
    }
    steps = []
    for name, command in real_plan():
        for old, new in swaps.items():
            command = command.replace(old, new)
        steps.append((name, command))
    return steps


def task_file_names(task, shards):
    tag = f"{task:05d}-of-{shards:05d}"
    return [
        f"make_examples.tfrecord-{tag}.gz",
        f"make_examples.tfrecord-{tag}.gz.example_info.json",
        f"make_examples_call_variant_outputs.tfrecord-{tag}.gz",
    ]


@pytest.fixture
def split_env(local_env, monkeypatch):
    """A BAM and a reference in local storage, a known DeepVariant version, the real plan, no real GPU."""
    (local_env / "alignments").mkdir(parents=True)
    (local_env / "alignments" / "S1.bam").write_bytes(b"BAM\x01fake")
    (local_env / "reference").mkdir()
    (local_env / "reference" / "ref.fa").write_text(">c\nACGT\n")
    (local_env / "reference" / "ref.fa.fai").write_text("c\t4\t3\t4\t5\n")
    monkeypatch.setattr(dv, "index_bam", lambda path: None)
    monkeypatch.setattr(dv, "gpu_name", lambda: None)
    monkeypatch.setattr(dv, "wrapper_version", lambda wrapper: "1.10.0")
    monkeypatch.setattr(dv, "planned_steps", plan_for)
    monkeypatch.setenv("RUN_NAME", RUN)
    monkeypatch.setenv("DEEPVARIANT_SHARDS", "4")
    for var in ("DEEPVARIANT_EXTRA_ARGS", "DEEPVARIANT_MODEL_TYPE", "PIPELINE_CALLER"):
        monkeypatch.delenv(var, raising=False)
    return local_env


def fake_make_examples(seen, write=None):
    """run_streaming for a part: makes each task's files for the `seq a b` it was given, as make_examples does."""

    def run(cmd, **_):
        command = cmd[-1]
        seen.append(command)
        first, last = map(int, re.search(r"seq (\d+) (\d+) \|", command).groups())
        shards = int(re.search(r"@(\d+)\.gz", command).group(1))
        inter = re.search(r'--examples "([^"]+)/make_examples', command).group(1)
        for task in write if write is not None else range(first, last + 1):
            for name in task_file_names(task, shards):
                with open(os.path.join(inter, name), "wb") as fh:
                    fh.write(b"x" * 10)
        events = [(1.0, f"\t{3 * (last - first + 1)} examples written"), (1.0, "\t2 small model examples called")]
        return 0, events, 5.0

    return run


def fake_gpu_steps(seen, code=0):
    """run_streaming for the GPU job's replayed steps; postprocess_variants writes the VCF."""

    def run(cmd, **_):
        seen.append(cmd[-1])
        if code == 0 and "postprocess_variants" in cmd[-1]:
            out = re.search(r'--outfile "([^"]+)"', cmd[-1]).group(1)
            with open(out, "wb") as fh:
                fh.write(gzip.compress(b"##fileformat=VCFv4.2\n"))
        return code, [(0.0, "log")], 1.0

    return run


def prefix_dir(root, run=RUN):
    return root / "intermediate" / "dv" / "S1" / run


def run_parts(monkeypatch, parts=2):
    seen = []
    monkeypatch.setattr(dv, "run_streaming", fake_make_examples(seen))
    for part in range(parts):
        dv.make_examples_part_task("S1", "ref.fa", part, parts)
    return seen


def metrics_lines(root):
    return [json.loads(line) for line in (root / "metrics" / "metrics.jsonl").read_text().splitlines()]


# ---- the pieces ----


@pytest.mark.parametrize(
    "parts, shards, expected",
    [(2, 4, [(0, 1), (2, 3)]), (2, 5, [(0, 1), (2, 4)]), (3, 3, [(0, 0), (1, 1), (2, 2)]), (1, 4, [(0, 3)])],
)
def test_parts_split_the_tasks_contiguously_and_evenly(parts, shards, expected):
    assert [dv.part_tasks(p, parts, shards) for p in range(parts)] == expected


@pytest.mark.parametrize("part, parts, shards", [(0, 5, 4), (2, 2, 4), (-1, 2, 4), (0, 0, 4)])
def test_impossible_parts_are_refused(part, parts, shards):
    with pytest.raises(ValueError):
        dv.part_tasks(part, parts, shards)


def test_a_part_runs_the_wrappers_own_make_examples_for_its_tasks_only():
    command = real_plan()[0][1]
    narrowed = dv.part_command(command, 2, 3, 4)
    # step_commands drops bash's "time " prefix
    assert narrowed.startswith("seq 2 3 | parallel -q --halt 2 --line-buffer /opt/deepvariant/bin/make_examples")
    assert narrowed.replace("seq 2 3", "seq 0 3") == command  # nothing else changes: same flags, same @4 output


@pytest.mark.parametrize(
    "command, shards",
    [
        ("time seq 0 3 | parallel make_examples --task {}", 8),  # planned for another task count
        ("time seq 0 3 | parallel make_examples --task 1", 4),  # not one task per {}
        ("time make_examples --examples x@4.gz", 4),  # no seq/parallel form at all
    ],
)
def test_an_unexpected_make_examples_command_is_refused(command, shards):
    with pytest.raises(ValueError, match="unexpected make_examples command"):
        dv.part_command(command, 0, 1, shards)


def test_the_gpu_job_replays_call_variants_and_postprocess_with_its_own_cpus():
    steps = dv.gpu_steps_from_plan(real_plan(), 16)
    assert [n for n, _ in steps] == ["call_variants", "postprocess_variants"]
    assert steps[0][1] == real_plan()[1][1]
    assert '--cpus "16"' in steps[1][1] and '--cpus "4"' not in steps[1][1]
    with pytest.raises(RuntimeError, match="unexpected DeepVariant plan"):
        dv.gpu_steps_from_plan(real_plan()[:2], 16)


# ---- a part job ----


def test_a_part_uploads_its_tasks_examples_and_then_its_manifest(split_env, monkeypatch):
    uploads = []
    store = dv.storage()
    real_upload, real_stream = store.upload, store.upload_stream
    monkeypatch.setattr(store, "upload", lambda path, key: (uploads.append(key), real_upload(path, key)))
    monkeypatch.setattr(store, "upload_stream", lambda s, key: (uploads.append(key), real_stream(s, key)))
    seen = []
    monkeypatch.setattr(dv, "run_streaming", fake_make_examples(seen))

    dv.make_examples_part_task("S1", "ref.fa", 1, 2)

    assert len(seen) == 1 and "seq 2 3 |" in seen[0]
    part_dir = prefix_dir(split_env) / "shard-1"
    assert sorted(os.listdir(part_dir)) == sorted(task_file_names(2, 4) + task_file_names(3, 4) + ["manifest.json"])
    assert uploads[-1].endswith("shard-1/manifest.json")  # last: a manifest means its files are all there
    manifest = json.loads((part_dir / "manifest.json").read_text())
    assert manifest["tasks"] == [2, 3] and manifest["parts"] == 2 and manifest["shards"] == 4
    assert manifest["deepvariant"] == "1.10.0" and manifest["examples"] == 6 and manifest["small_model_calls"] == 2
    assert manifest["alignment"][0] == "S1.bam" and manifest["reference"][0] == "ref.fa"
    assert manifest["run_name"] == RUN and len(manifest["files"]) == 6
    assert {m["task"] for m in metrics_lines(split_env)} == {"DeepVariantExamples"}
    assert not (split_env / "runs" / "S1.provenance" / "DeepVariantExamples.json").exists()  # no run-record part


def test_a_part_that_wrote_the_wrong_tasks_fails_without_a_manifest(split_env, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", fake_make_examples([], write=[2]))  # task 3 missing
    with pytest.raises(RuntimeError, match="expected 2-3"):
        dv.make_examples_part_task("S1", "ref.fa", 1, 2)
    assert not (prefix_dir(split_env) / "shard-1" / "manifest.json").exists()


def test_a_part_needs_the_deepvariant_version(split_env, monkeypatch):
    monkeypatch.setattr(dv, "wrapper_version", lambda wrapper: None)
    monkeypatch.setattr(dv, "run_streaming", fake_make_examples([]))
    with pytest.raises(RuntimeError, match="DeepVariant version"):
        dv.make_examples_part_task("S1", "ref.fa", 0, 2)


def test_parts_default_to_their_cpus_worth_of_tasks_each(split_env, monkeypatch):
    monkeypatch.delenv("DEEPVARIANT_SHARDS")
    monkeypatch.setattr(dv.os, "cpu_count", lambda: 3)
    seen = []
    monkeypatch.setattr(dv, "run_streaming", fake_make_examples(seen))
    dv.make_examples_part_task("S1", "ref.fa", 1, 2)
    assert "seq 3 5 |" in seen[0] and "@6.gz" in seen[0]


# ---- the GPU job ----


def test_the_gpu_job_calls_every_parts_examples_and_writes_the_usual_outputs(split_env, monkeypatch):
    run_parts(monkeypatch)
    seen = []
    monkeypatch.setattr(dv, "run_streaming", fake_gpu_steps(seen))
    monkeypatch.setattr(dv.os, "cpu_count", lambda: 16)

    dv.deepvariant_from_examples_task("S1", "ref.fa")

    assert [re.search(r"/(call_variants|postprocess_variants) ", c).group(1) for c in seen] == [
        "call_variants",
        "postprocess_variants",
    ]
    assert '--cpus "16"' in seen[1] and "@4.gz" in seen[0]
    assert (split_env / "variants_deepvariant" / "S1.vcf.gz").exists()  # the same key as the single job
    recorded = {m["task"]: m for m in metrics_lines(split_env)}
    assert {"DeepVariant", "DeepVariant.MakeExamples", "DeepVariant.CallVariants"} <= set(recorded)
    seconds = [
        json.loads((prefix_dir(split_env) / f"shard-{p}" / "manifest.json").read_text())["make_examples_seconds"]
        for p in range(2)
    ]
    assert recorded["DeepVariant.MakeExamples"]["duration_seconds"] == pytest.approx(max(seconds))  # the slowest part
    part = json.loads((split_env / "runs" / "S1.provenance" / "DeepVariant.json").read_text())
    assert part["status"] == "Success"
    assert part["make_examples"]["split"] and part["make_examples"]["parts"] == 2
    assert part["make_examples"]["examples"] == 12
    assert any(i["key"].startswith(f"intermediate/dv/S1/{RUN}/") for i in part["inputs"])


def test_the_gpu_job_does_not_download_the_alignment(split_env, monkeypatch):
    run_parts(monkeypatch)
    monkeypatch.setattr(dv, "run_streaming", fake_gpu_steps([]))
    downloads = []
    store = dv.storage()
    real = store.download
    monkeypatch.setattr(store, "download", lambda key, path: (downloads.append(key), real(key, path)))
    dv.deepvariant_from_examples_task("S1", "ref.fa")
    assert not any(k.startswith("alignments/") for k in downloads)


def write_manifest(root, slot, **fields):
    """A part's manifest in shard-<slot>/; fields override the defaults (including "part")."""
    manifest = {
        "split_version": 1,
        "deepvariant": "1.10.0",
        "shards": 4,
        "parts": 2,
        "part": slot,
        "tasks": [0, 1] if slot == 0 else [2, 3],
        "model_type": "WGS",
        "extra_args": "",
        "reference": ["ref.fa", 9, "r"],
        "alignment": ["S1.bam", 9, "a"],
        "files": [],
        **fields,
    }
    path = prefix_dir(root) / f"shard-{slot}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "manifests, env, match",
    [
        ([], {}, "no make_examples parts"),
        ([(0, {})], {}, r"parts \[1\] of 2 are missing"),
        ([(0, {}), (1, {"shards": 8})], {}, "disagree on shards"),
        ([(0, {}), (1, {"alignment": ["S1.bam", 9, "b"]})], {}, "disagree on alignment"),
        ([(0, {}), (1, {"tasks": [1, 3]})], {}, "exactly once"),
        ([(0, {}), (1, {})], {"DEEPVARIANT_EXTRA_ARGS": "--call_variants_extra_args=batch_size=256"}, "extra_args"),
        ([(0, {"deepvariant": "1.9.0"}), (1, {"deepvariant": "1.9.0"})], {}, "deepvariant"),
        ([(0, {"split_version": 9}), (1, {})], {}, "split_version"),
        ([(0, {}), (1, {"part": None})], {}, "no part number"),
    ],
)
def test_the_gpu_job_refuses_incomplete_or_inconsistent_parts_before_any_work(
    split_env, monkeypatch, manifests, env, match
):
    for part, fields in manifests:
        write_manifest(split_env, part, **fields)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    called = []
    monkeypatch.setattr(dv, "run_streaming", lambda *a, **k: called.append(a))
    monkeypatch.setattr(dv, "ensure_reference_local", lambda *a, **k: called.append("reference"))
    with pytest.raises(ValueError, match=match):
        dv.deepvariant_from_examples_task("S1", "ref.fa")
    assert called == []  # no reference download, no DeepVariant step


def test_the_gpu_job_refuses_parts_made_with_another_reference(split_env, monkeypatch):
    run_parts(monkeypatch)
    (split_env / "reference" / "ref.fa").write_text(">c\nACGTACGT\n")
    monkeypatch.setattr(dv, "run_streaming", fake_gpu_steps([]))
    with pytest.raises(ValueError, match="differs from the one the parts used"):
        dv.deepvariant_from_examples_task("S1", "ref.fa")


def test_another_runs_parts_are_never_used(split_env, monkeypatch):
    run_parts(monkeypatch)
    monkeypatch.setenv("RUN_NAME", "S1-another-run")
    with pytest.raises(ValueError, match="no make_examples parts"):
        dv.deepvariant_from_examples_task("S1", "ref.fa")


def test_a_failed_gpu_step_fails_the_stage_naming_it(split_env, monkeypatch):
    run_parts(monkeypatch)
    monkeypatch.setattr(dv, "run_streaming", fake_gpu_steps([], code=1))
    with pytest.raises(RuntimeError, match="call_variants exited with code 1"):
        dv.deepvariant_from_examples_task("S1", "ref.fa")
    assert not (split_env / "variants_deepvariant" / "S1.vcf.gz").exists()


# ---- the CLI and run_local ----


@pytest.mark.parametrize(
    "argv, match",
    [
        (["S1", "ref.fa", "--make-examples-part", "0"], "go together"),
        (["S1", "ref.fa", "--parts", "2"], "go together"),
        (["S1", "ref.fa", "--from-examples", "--pangenome", "g.gbz"], "linear DeepVariant only"),
        (["S1", "ref.fa", "--from-examples", "--make-examples-part", "0", "--parts", "2"], "not allowed with"),
    ],
)
def test_the_cli_refuses_incomplete_or_conflicting_split_options(argv, match, capsys):
    with pytest.raises(SystemExit):
        dv.main(argv)
    assert re.search(match, capsys.readouterr().err)


def test_run_local_runs_the_parts_on_the_cpu_image_then_the_gpu_job():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", caller="deepvariant", skip_to_variants=True, dv_split=3)
    names = [j["name"] for j in jobs]
    assert names == ["deepvariant-examples"] * 3 + ["deepvariant"]
    parts = jobs[:3]
    assert all(j["image"] == "geyser-deepvariant-cpu" and j["gpus"] is False for j in parts)
    assert [j["cmd"][-4:] for j in parts] == [["--make-examples-part", str(p), "--parts", "3"] for p in range(3)]
    assert jobs[-1]["cmd"][-1] == "--from-examples" and jobs[-1]["image"] == "geyser-deepvariant"
    assert "deepvariant-examples" in run_local.DEEPVARIANT_JOBS  # gets DEEPVARIANT_* and --dv-memory


def test_run_local_without_split_is_unchanged():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", caller="deepvariant", skip_to_variants=True)
    assert [j["name"] for j in jobs] == ["deepvariant"] and "--from-examples" not in jobs[0]["cmd"]
