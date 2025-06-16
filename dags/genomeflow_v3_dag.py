# /dags/genomeflow_v3_dag.py
from __future__ import annotations

import pendulum

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

# ----------------------------------------------------
# Define all file paths FIRST. Note the correct order.
# ----------------------------------------------------
DATA_DIR = "/opt/airflow/data"
RAW_READS_DIR = f"{DATA_DIR}/raw_reads"
DECOMPRESSED_DIR = f"{DATA_DIR}/decompressed"
QC_REPORTS_DIR = f"{DATA_DIR}/qc_reports"  # New directory for QC results

# ----------------------------------------------------
# Now, define the DAG
# ----------------------------------------------------
with DAG(
    dag_id="genomeflow_pipeline_v3",
    start_date=pendulum.datetime(2025, 6, 14, tz="UTC"),
    catchup=False,
    schedule=None,
    doc_md="""
    ### GenomeFlow Pipeline v3
    - V3: Adds real Quality Control using FastQC.
    """,
    tags=["genomeflow", "bioinformatics", "fastqc"],
) as dag:
    # ----------------------------------------------------
    # ALL TASKS are indented at the same level inside the DAG block.
    # ----------------------------------------------------

    # Task 0: Create ALL output directories needed for the pipeline.
    create_output_directories = BashOperator(
        task_id="create_output_directories",
        bash_command=f"mkdir -p {DECOMPRESSED_DIR} {QC_REPORTS_DIR}",
    )

    # Task 1: Decompress the raw sequencing data.
    decompress_sample = BashOperator(
        task_id="decompress_sample",
        bash_command=f"""
            INPUT_FILE={RAW_READS_DIR}/sample_1.fastq.gz
            OUTPUT_FILE={DECOMPRESSED_DIR}/sample_1.fastq
            if [ -f "$OUTPUT_FILE" ]; then
                echo "Output file $OUTPUT_FILE already exists. Skipping."
            else
                echo "Decompressing $INPUT_FILE to $OUTPUT_FILE..."
                gunzip -c "$INPUT_FILE" > "$OUTPUT_FILE"
            fi
        """,
    )

      # Task 2: Run quality control using FastQC
    run_quality_control = BashOperator(
        task_id="run_quality_control",
        bash_command=f"""
            INPUT_FILE="{DECOMPRESSED_DIR}/sample_1.fastq"
            OUTPUT_DIR="{QC_REPORTS_DIR}"
            # THE FIX IS HERE: Use the Python variable for the directory path.
            EXPECTED_REPORT="{QC_REPORTS_DIR}/sample_1_fastqc.html"

            # The rest of the script uses Bash variables, which is correct.
            if [ -f "$EXPECTED_REPORT" ]; then
                echo "FastQC report '$EXPECTED_REPORT' already exists. Skipping."
            else
                echo "Running FastQC on '$INPUT_FILE'..."
                fastqc "$INPUT_FILE" -o "$OUTPUT_DIR"
                echo "FastQC analysis complete."
            fi
        """,
    )


    # Task 3: A placeholder for the next step.
    align_genome = BashOperator(
        task_id="align_genome",
        bash_command='echo "Step 3: Aligning reads to reference genome..."',
    )

    # ----------------------------------------------------
    # The dependency chain is also indented inside the DAG block.
    # ----------------------------------------------------
    create_output_directories >> decompress_sample >> run_quality_control >> align_genome

