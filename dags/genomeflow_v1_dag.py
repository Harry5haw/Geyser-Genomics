# dags/genomeflow_v1_dag.py
from __future__ import annotations

import pendulum

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="genomeflow_pipeline_v1",
    start_date=pendulum.datetime(2025, 6, 14, tz="UTC"),
    catchup=False,
    schedule=None,
    tags=["genomeflow", "simulation"],
) as dag:
    # Task 1: Simulate decompressing the raw sequencing data
    decompress_sample = BashOperator(
        task_id="decompress_sample",
        bash_command='echo "Step 1: Decompressing sample FASTQ files..."',
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

    # Define the task dependency chain for the pipeline
    decompress_sample >> run_quality_control >> align_genome
