"""AWS Glue entry point for scripts/cohort_runs.py: the cohort table from runs/*.provenance.json, as a Glue job.

Glue 5.0 (Spark 3.5.4, Python 3.11) matches requirements-cohort.txt. Upload cohort_runs.py next to this script and
pass it with --extra-py-files; the job arguments are --RUNS (s3://<bucket>/runs) and --OUT (s3://<bucket>/cohort/runs),
plus --CSV true for a CSV copy. docs/cohort-qc.md has the commands. Nothing in Terraform creates this job: it is an
on-demand analysis, not part of the pipeline.
"""

import sys

from awsglue.context import GlueContext  # provided by the Glue runtime
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

import cohort_runs

required = ["JOB_NAME", "RUNS", "OUT"]
optional = ["CSV"] if "--CSV" in sys.argv else []
args = getResolvedOptions(sys.argv, required + optional)

glue = GlueContext(SparkContext.getOrCreate())
job = Job(glue)
job.init(args["JOB_NAME"], args)
rows = cohort_runs.build_cohort(
    glue.spark_session, args["RUNS"], args["OUT"], csv=args.get("CSV", "false").lower() == "true"
)
print(f"cohort: {rows} samples -> {args['OUT']}")
job.commit()
