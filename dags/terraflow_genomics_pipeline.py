from __future__ import annotations
import pendulum
from airflow.models.dag import DAG
from airflow.models.param import Param
from airflow.providers.amazon.aws.operators.batch import BatchOperator

with DAG(
    dag_id="terraflow_genomics_pipeline",
    start_date=pendulum.datetime(2025, 9, 11, tz="UTC"),
    catchup=False,
    schedule=None,
    doc_md="""
    ### TerraFlow Genomics Pipeline (Dynamic & Parameterized)
    Orchestrates a bioinformatics workflow using AWS Batch.
    **Trigger this DAG with a configuration** to specify the Sample ID and Reference Genome to process.
    """,
    params={
        "srr_id": Param(
            type="string",
            title="Sample ID",
            description="The sample ID to process (e.g., small_test).",
            default="small_test",
        ),
        "reference_name": Param(
            type="string",
            title="Reference Genome Filename",
            description="The FASTA filename of the reference genome in S3 (e.g., chr20.fa).",
            default="chr20.fa",
        )
    },
    tags=["bioinformatics", "aws", "batch", "terraflow"],
) as dag:

    # Define common parameters for all Batch jobs to keep the code DRY
    common_batch_params = {
        "job_queue": "teraflow-job-queue",
        "job_definition": "teraflow-app-job",
        "aws_conn_id": "aws_default",
        "region_name": "eu-west-2",
        "wait_for_completion": True,
    }
    
    # Use Jinja templating to access the parameters from the DAG run configuration
    srr_id_param = "{{ params.srr_id }}"
    ref_name_param = "{{ params.reference_name }}"

    # --- Task 1: Decompress Sample ---
    task_decompress = BatchOperator(
        task_id="decompress_job",
        job_name=f"decompress-{srr_id_param}",
        overrides={"command": ["decompress", srr_id_param]},
        **common_batch_params,
    )

    # --- Task 2: Quality Control ---
    task_qc = BatchOperator(
        task_id="quality_control_job",
        job_name=f"qc-{srr_id_param}",
        overrides={"command": ["qc", srr_id_param]},
        **common_batch_params,
    )

    # --- Task 3: Align Genome ---
    task_align = BatchOperator(
        task_id="align_genome_job",
        job_name=f"align-{srr_id_param}",
        overrides={"command": ["align", srr_id_param, ref_name_param]},
        **common_batch_params,
    )

    # --- Task 4: Call Variants ---
    task_variants = BatchOperator(
        task_id="call_variants_job",
        job_name=f"variants-{srr_id_param}",
        overrides={"command": ["variants", srr_id_param, ref_name_param]},
        do_xcom_push=True,
        **common_batch_params,
    )

    # --- Define Dependencies ---
    task_decompress >> task_qc >> task_align >> task_variants
