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
    start_date=pendulum.datetime(2025, 6, 17, tz="UTC"),
    catchup=False,
    schedule=None,
    doc_md="""
    ### TerraFlow Genomics Pipeline
    Orchestrates a bioinformatics workflow using AWS Batch. Each task is a stateless
    job that reads from and writes to S3, executed by a container from ECR.
    """,
    tags=["bioinformatics", "aws", "batch", "terratflow"],
) as dag:

    # --- Task 1: Decompress Sample ---
    # This task submits a job to AWS Batch that will run our tasks.py script
    # with the 'decompress' argument.
    task_decompress = BatchOperator(
        task_id="decompress_job",
        job_name=f"decompress-{SRR_ID}",
        job_queue="genomeflow-job-queue",      # The queue we created with Terraform
        job_definition="genomeflow-job-definition", # The definition we created with Terraform
        overrides={
            "command": ["python", "tasks.py", "decompress", SRR_ID],
        },
        aws_conn_id="aws_default", # Tells Airflow to use the default AWS connection
    )

    # --- Task 2: Quality Control ---
    task_qc = BatchOperator(
        task_id="quality_control_job",
        job_name=f"qc-{SRR_ID}",
        job_queue="genomeflow-job-queue",
        job_definition="genomeflow-job-definition",
        overrides={
            "command": ["python", "tasks.py", "qc", SRR_ID],
        },
        aws_conn_id="aws_default",
    )

    # --- Task 3: Align Genome ---
    task_align = BatchOperator(
        task_id="align_genome_job",
        job_name=f"align-{SRR_ID}",
        job_queue="genomeflow-job-queue",
        job_definition="genomeflow-job-definition",
        overrides={
            "command": ["python", "tasks.py", "align", SRR_ID],
        },
        aws_conn_id="aws_default",
    )

    # --- Task 4: Call Variants ---
    task_variants = BatchOperator(
        task_id="call_variants_job",
        job_name=f"variants-{SRR_ID}",
        job_queue="genomeflow-job-queue",
        job_definition="genomeflow-job-definition",
        overrides={
            "command": ["python", "tasks.py", "variants", SRR_ID],
        },
        aws_conn_id="aws_default",
    )

    # --- Define Dependencies ---
    # This defines the sequential order of our entire pipeline.
    task_decompress >> task_qc >> task_align >> task_variants

