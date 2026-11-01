#!/usr/bin/env python3
"""Run the whole pipeline on this machine, with no AWS involved.

This plays the part of the cloud orchestration:
  * Lambda trigger  -> the sample ID is parsed from raw_reads/<id>.fastq.gz using the
                       same function the Lambda uses (lambda/trigger/index.py).
  * Step Functions  -> the four stages run in the same order with the same commands
                       (infrastructure/step_functions.tf); the first failure stops the run.
  * AWS Batch       -> each stage runs in a fresh container from the same image.
  * S3              -> a local folder (default ./local_bucket) mounted at /data.

Typical use:
    docker build -f Dockerfile -t geyser-app .
    python scripts/make_test_data.py
    python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --check

--check compares the VCF against truth/<sample>.json written by make_test_data.py.
"""

import argparse
import glob
import gzip
import importlib.util
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Same order and arguments as the Step Functions definition.
STAGES = [
    ("decompress", False),
    ("qc", False),
    ("align", True),
    ("variants", True),
]


def load_trigger():
    """Import lambda/trigger/index.py by path ('lambda' is a Python keyword, so no normal import)."""
    path = os.path.join(REPO_ROOT, "lambda", "trigger", "index.py")
    spec = importlib.util.spec_from_file_location("geyser_trigger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage_command(stage, sample_id, reference_name, needs_reference):
    cmd = ["python", "tasks.py", stage, sample_id]
    if needs_reference:
        cmd.append(reference_name)
    return cmd


def run_stage(cmd, runner, root, image):
    env = {"STORAGE_BACKEND": "local", "METRICS_BACKEND": "local"}
    if runner == "docker":
        full = ["docker", "run", "--rm",
                "--mount", f"type=bind,source={os.path.abspath(root)},target=/data"]
        for k, v in {**env, "LOCAL_STORAGE_DIR": "/data"}.items():
            full += ["-e", f"{k}={v}"]
        full += [image] + cmd
        return subprocess.run(full).returncode
    # host: tools (bwa, samtools, bcftools, fastqc) must be on PATH
    host_env = {**os.environ, **env, "LOCAL_STORAGE_DIR": os.path.abspath(root)}
    return subprocess.run([sys.executable, "tasks.py"] + cmd[2:], cwd=os.path.join(REPO_ROOT, "app"),
                          env=host_env).returncode


def read_vcf_calls(path):
    """Return {(pos, ref, alt)} from a (gzipped) VCF."""
    calls = set()
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            for alt in cols[4].split(","):
                calls.add((int(cols[1]), cols[3], alt))
    return calls


def check_against_truth(root, sample_id):
    truth_path = os.path.join(root, "truth", f"{sample_id}.json")
    vcf_path = os.path.join(root, "variants", f"{sample_id}.vcf.gz")
    with open(truth_path, encoding="utf-8") as fh:
        truth = {(s["pos"], s["ref"], s["alt"]) for s in json.load(fh)["snps"]}
    called = read_vcf_calls(vcf_path)
    missed, extra = truth - called, called - truth
    print(f"\nTruth check: {len(truth & called)}/{len(truth)} planted SNPs called, {len(extra)} unexpected calls")
    for item in sorted(missed):
        print(f"  MISSED   pos={item[0]} {item[1]}>{item[2]}")
    for item in sorted(extra):
        print(f"  EXTRA    pos={item[0]} {item[1]}>{item[2]}")
    return not missed and not extra


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--sample", help="sample ID; default: every raw_reads/*.fastq.gz under --root")
    p.add_argument("--reference", help="reference name under reference/ (default: same as the Lambda)")
    p.add_argument("--runner", choices=["docker", "host"], default="docker")
    p.add_argument("--image", default="geyser-app")
    p.add_argument("--check", action="store_true", help="compare the VCF with truth/<sample>.json")
    a = p.parse_args(argv)

    trigger = load_trigger()
    if a.sample:
        keys = [f"raw_reads/{a.sample}.fastq.gz"]
    else:
        keys = [os.path.relpath(f, a.root).replace(os.sep, "/")
                for f in sorted(glob.glob(os.path.join(a.root, "raw_reads", "*.gz")))]
    if not keys:
        print(f"No inputs found under {a.root}/raw_reads/. Run scripts/make_test_data.py first.")
        return 1

    ok = True
    for key in keys:
        payload = trigger.build_pipeline_input(key, a.reference)
        sample_id, reference = payload["srr_id"], payload["reference_name"]
        print(f"\n=== Pipeline input: {json.dumps(payload)} ===")
        started = time.time()
        for stage, needs_ref in STAGES:
            cmd = stage_command(stage, sample_id, reference, needs_ref)
            print(f"\n>>> {' '.join(cmd)}", flush=True)
            if run_stage(cmd, a.runner, a.root, a.image) != 0:
                print(f"\nFAILED at stage '{stage}' for {sample_id} (in the cloud this goes to Notify_Failure)")
                ok = False
                break
        else:
            print(f"\n=== {sample_id} finished in {time.time() - started:.1f}s; "
                  f"VCF at {os.path.join(a.root, 'variants', sample_id + '.vcf.gz')} ===")
            if a.check:
                ok = check_against_truth(a.root, sample_id) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
