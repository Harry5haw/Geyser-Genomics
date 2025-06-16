# dags/genomeflow_v1_dag.py
from __future__ import annotations

import pendulum

# Define file paths relative to the Airflow container's internal filesystem
DATA_DIR = "/opt/airflow/data"
RAW_READS_DIR = f"{DATA_DIR}"
DECOMPRESSED_DIR = f"{DATA_DIR}/decompressed"

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

with DAG(
    
    dag_id="genomeflow_pipeline_v2",
    start_date=pendulum.datetime(2025, 6, 14, tz="UTC"),
    catchup=False,
    schedule=None,
    tags=["genomeflow", "simulation"],
) as dag:
        # Task to ensure the output directory exists before we try to write to it.
    create_output_directory = BashOperator(
        task_id="create_decompressed_dir",
        bash_command=f"mkdir -p {DECOMPRESSED_DIR}",
    )

   # Task 1: Decompress the sample. This is the task you are modifying.
    decompress_sample = BashOperator(
        task_id="decompress_sample",
        bash_command=f"""
            echo "Decompressing {RAW_READS_DIR}/sample_1.fastq.gz to {DECOMPRESSED_DIR}/sample_1.fastq"
            gunzip -c {RAW_READS_DIR}/sample_1.fastq.gz > {DECOMPRESSED_DIR}/sample_1.fastq
        """,
    )
    # Task 2: Simulate running quality control on the raw data
    run_quality_control = BashOperator(
        task_id="run_quality_control",
        bash_command='echo "Step 2: Running FastQC for quality control..."',
    )

    # Task 3: Simulate aligning reads to a reference genome
    align_genome = BashOperator(
        task_id="align_genome",
        bash_command='echo "Step 3: Aligning reads to reference genome..."',
    )

  # And finally, you modify the dependency chain, also inside the block.
    create_output_directory >> decompress_sample >> run_quality_control >> align_genome
    