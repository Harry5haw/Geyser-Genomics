# /dags/genomeflow_v3_dag.py
from __future__ import annotations

import pendulum

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

# ----------------------------------------------------
# Define all file paths FIRST. Note the correct order.
# ----------------------------------------------------

DATA_DIR = "/opt/airflow/data"
RAW_READS_DIR = f"{DATA_DIR}/raw_reads"# We can simplify this later, but it's fine for now
DECOMPRESSED_DIR = f"{DATA_DIR}/decompressed"
QC_REPORTS_DIR = f"{DATA_DIR}/qc_reports"  # New directory for QC results
ALIGNMENTS_DIR = f"{DATA_DIR}/alignments"
VARIANTS_DIR = f"{DATA_DIR}/variants"


# ----------------------------------------------------
# Now, define the DAG
# ----------------------------------------------------
with DAG(
    dag_id="genomeflow_pipeline_v5",
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
    bash_command=f"mkdir -p {DECOMPRESSED_DIR} {QC_REPORTS_DIR} {ALIGNMENTS_DIR} {VARIANTS_DIR}",
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

    # Task 3: Align reads to the reference genome using BWA-MEM and Samtools
    align_genome = BashOperator(
    task_id="align_genome",
    bash_command=f"""
        REF_GENOME="{DATA_DIR}/reference/reference.fa"
        INPUT_FILE="{DECOMPRESSED_DIR}/sample_1.fastq"
        OUTPUT_FILE="{ALIGNMENTS_DIR}/sample_1.bam"

        if [ -f "$OUTPUT_FILE" ]; then
            echo "Alignment file $OUTPUT_FILE already exists. Skipping."
        else
            echo "Running BWA-MEM to align $INPUT_FILE to $REF_GENOME..."
            # bwa mem outputs a SAM file to the screen (stdout)
            # We pipe (|) that directly to samtools view to convert it to a BAM file
            bwa mem "$REF_GENOME" "$INPUT_FILE" | samtools view -S -b > "$OUTPUT_FILE"
            echo "Alignment complete. Output at $OUTPUT_FILE"
        fi
    """,
)

# Task 4: Call variants and flag the mutation of interest
    call_and_flag_variants = BashOperator(
    task_id="call_and_flag_variants",
    bash_command=f"""
        REF_GENOME="{DATA_DIR}/reference/reference.fa"
        INPUT_FILE="{ALIGNMENTS_DIR}/sample_1.bam"
        OUTPUT_FILE="{VARIANTS_DIR}/sample_1.vcf"
        GENE_OF_INTEREST="chr22"

        echo "Calling variants with bcftools..."
        # bcftools mpileup requires the reference genome (-f)
        # We pipe (|) its output to bcftools call to generate a VCF file
        # The -m flag in 'call' enables multiallelic and rare variant calling
        bcftools mpileup -f "$REF_GENOME" "$INPUT_FILE" | bcftools call -mv -o "$OUTPUT_FILE"

        echo "Searching for variants in gene: $GENE_OF_INTEREST..."
        # Use grep to find the line with our gene. The exit code of grep will determine task success.
        # If it finds the gene, grep exits with 0 (success). If not, it exits with 1 (failure).
        grep "$GENE_OF_INTEREST" "$OUTPUT_FILE"

        echo "--- PIPELINE COMPLETE: Mutation of interest found and flagged! ---"
    """,
)



    # ----------------------------------------------------
    # The dependency chain is also indented inside the DAG block.
    # ----------------------------------------------------
create_output_directories >> decompress_sample >> run_quality_control >> align_genome >> call_and_flag_variants

