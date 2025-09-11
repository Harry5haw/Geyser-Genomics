# /dags/terraflow_genomics_pipeline.py
from __future__ import annotations
import pendulum
from airflow.models.dag import DAG
from airflow.providers.amazon.aws.operators.batch import BatchOperator

# A constant for the sample ID we are processing
SRR_ID = "SRR062634"

# --- DAG Definition ---
with DAG(
    dag_id="terraflow_genomics_pipeline",
    start_date=pendulum.datetime(2025, 9, 11, tz="UTC"), # Updated to today
    catchup=False,
    schedule=None,
    doc_md="""
    ### TerraFlow Genomics Pipeline
    Orchestrates a bioinformatics workflow using AWS Batch. Each task is a stateless
    job that reads from and writes to S3, executed by a container from ECR.
    """,
    tags=["bioinformatics", "aws", "batch", "terraflow"],
) as dag:

    # --- Task 1: Decompress Sample ---
    task_decompress = BatchOperator(
        task_id="decompress_job",
        job_name=f"decompress-{SRR_ID}",
        job_queue="teraflow-job-queue",          # CORRECTED: Matches Terraform output
        job_definition="teraflow-app-job",       # CORRECTED: Matches Terraform output
        overrides={
            "command": ["decompress", SRR_ID],   # CORRECTED: Command is now arguments only
        },
        aws_conn_id="aws_default", # Tells Airflow to use the default AWS connection
        region_name="eu-west-2",   # Best practice: specify region
    )

    # --- Task 2: Quality Control ---
    task_qc = BatchOperator(
        task_id="quality_control_job",
        job_name=f"qc-{SRR_ID}",
        job_queue="teraflow-job-queue",          # CORRECTED
        job_definition="teraflow-app-job",       # CORRECTED
        overrides={
            "command": ["qc", SRR_ID],           # CORRECTED
        },
        aws_conn_id="aws_default",
        region_name="eu-west-2",
    )

    # --- Task 3: Align Genome ---
    task_align = BatchOperator(
        task_id="align_genome_job",
        job_name=f"align-{SRR_ID}",
        job_queue="teraflow-job-queue",          # CORRECTED
        job_definition="teraflow-app-job",       # CORRECTED
        overrides={
            "command": ["align", SRR_ID],        # CORRECTED
        },
        aws_conn_id="aws_default",
        region_name="eu-west-2",
    )

    # --- Task 4: Call Variants ---
    task_variants = BatchOperator(
        task_id="call_variants_job",
        job_name=f"variants-{SRR_ID}",
        job_queue="teraflow-job-queue",          # CORRECTED
        job_definition="teraflow-app-job",       # CORRECTED
        overrides={
            "command": ["variants", SRR_ID],     # CORRECTED
        },
        aws_conn_id="aws_default",
        region_name="eu-west-2",
    )

    # --- Define Dependencies ---
    # This defines the sequential order of our entire pipeline.
    task_decompress >> task_qc >> task_align >> task_variants

