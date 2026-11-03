#!/usr/bin/env python3
"""Stop a pipeline run: its Step Functions execution AND the Batch jobs it started.

Stopping an execution does not stop the Batch jobs its `.sync` tasks submitted: they keep running (and
billing) to the end. This stops the execution first, so it submits nothing new, then terminates every job of
that run still RUNNABLE, STARTING or RUNNING in the pipeline's job queues. A job belongs to the run when its
name ends with "-<execution name>": the state machine names every job `<Stage>-<sample or shard>-<execution
name>` (infrastructure/state_machine.asl.json; a test checks the formats).

    python scripts/stop_run.py CLOUD014-20261102T200023Z-1d9aceed --dry-run   # what it would stop
    python scripts/stop_run.py CLOUD014-20261102T200023Z-1d9aceed             # stop it

The state machine comes from --state-machine-arn or $SFN_ARN; a full execution ARN works too. --dry-run makes
only read calls (describe/list). Region defaults to eu-west-2. Exit code 0 when nothing of the run is left
running, 1 if the execution doesn't exist, 2 if some job could not be terminated.
"""

import argparse
import os
import sys

DEFAULT_REGION = "eu-west-2"
ACTIVE_JOB_STATES = ("RUNNABLE", "STARTING", "RUNNING")
DEFAULT_REASON = "Run stopped by scripts/stop_run.py"


def execution_arn(name_or_arn: str, state_machine_arn: str = None) -> str:
    """The execution ARN from its name and the state machine ARN (or the execution ARN itself)."""
    if name_or_arn.startswith("arn:"):
        if ":execution:" not in name_or_arn:
            raise ValueError(f"not an execution ARN: {name_or_arn}")
        return name_or_arn
    if not state_machine_arn:
        raise ValueError("give --state-machine-arn (or set SFN_ARN), or a full execution ARN")
    if ":stateMachine:" not in state_machine_arn:
        raise ValueError(f"not a state machine ARN: {state_machine_arn}")
    return state_machine_arn.replace(":stateMachine:", ":execution:", 1) + ":" + name_or_arn


def execution_name(arn: str) -> str:
    return arn.rsplit(":", 1)[1]


def job_queues(batch) -> list:
    """Every job queue's name (the pipeline's Fargate queue and the GPU queue)."""
    queues, token = [], None
    while True:
        page = batch.describe_job_queues(**({"nextToken": token} if token else {}))
        queues += [q["jobQueueName"] for q in page.get("jobQueues", [])]
        token = page.get("nextToken")
        if not token:
            return queues


def active_jobs(batch, name: str, queues=None) -> list:
    """[{jobId, jobName, status, queue}] of this run's jobs that are still queued or running."""
    suffix = f"-{name}"
    found = []
    for queue in queues if queues is not None else job_queues(batch):
        for status in ACTIVE_JOB_STATES:
            token = None
            while True:
                kwargs = {"jobQueue": queue, "jobStatus": status}
                if token:
                    kwargs["nextToken"] = token
                page = batch.list_jobs(**kwargs)
                for job in page.get("jobSummaryList", []):
                    if job.get("jobName", "").endswith(suffix):
                        found.append(
                            {"jobId": job["jobId"], "jobName": job["jobName"], "status": status, "queue": queue}
                        )
                token = page.get("nextToken")
                if not token:
                    break
    return found


def stop_run(sfn, batch, arn: str, dry_run: bool = False, reason: str = DEFAULT_REASON, out=print) -> int:
    """Stop the execution (if running) and terminate its active jobs. Returns the exit code (see the docstring)."""
    name = execution_name(arn)
    try:
        status = sfn.describe_execution(executionArn=arn)["status"]
    except sfn.exceptions.ExecutionDoesNotExist:
        out(f"No execution {name}")
        return 1
    verb = "Would stop" if dry_run else "Stopping"
    if status == "RUNNING":
        out(f"{verb} execution {name} (RUNNING)")
        if not dry_run:
            sfn.stop_execution(executionArn=arn, error="StoppedByOperator", cause=reason)
    else:
        out(f"Execution {name} is already {status}")
    jobs = active_jobs(batch, name)
    if not jobs:
        out("No queued or running Batch jobs for this run")
        return 0
    failed = 0
    for job in jobs:
        out(f"{'Would terminate' if dry_run else 'Terminating'} {job['jobName']} ({job['status']}, {job['queue']})")
        if dry_run:
            continue
        try:
            batch.terminate_job(jobId=job["jobId"], reason=reason)
        except Exception as err:  # noqa: BLE001 - keep going: terminate every other job of the run
            failed += 1
            out(f"  could not terminate {job['jobId']}: {err}")
    if dry_run:
        out(f"Dry run: {len(jobs)} job(s) would be terminated; nothing was changed")
        return 0
    return 2 if failed else 0


def main(argv=None, sfn_client=None, batch_client=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("execution", help="execution name (e.g. CLOUD014-20261102T200023Z-1d9aceed) or execution ARN")
    p.add_argument("--state-machine-arn", default=os.environ.get("SFN_ARN"), help="default: $SFN_ARN")
    p.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})")
    p.add_argument("--dry-run", action="store_true", help="only read: show what would be stopped")
    p.add_argument("--reason", default=DEFAULT_REASON, help="reason recorded on the execution and the jobs")
    a = p.parse_args(argv)
    try:
        arn = execution_arn(a.execution, a.state_machine_arn)
    except ValueError as err:
        p.error(str(err))
    if sfn_client is None or batch_client is None:
        import boto3  # noqa: PLC0415 - only for real runs; tests pass stubbed clients

        sfn_client = sfn_client or boto3.client("stepfunctions", region_name=a.region)
        batch_client = batch_client or boto3.client("batch", region_name=a.region)
    return stop_run(sfn_client, batch_client, arn, dry_run=a.dry_run, reason=a.reason)


if __name__ == "__main__":
    sys.exit(main())
