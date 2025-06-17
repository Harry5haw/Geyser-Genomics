# /dags/genomeflow_v3_dag.py
from __future__ import annotations

import pendulum
import subprocess
import boto3
from botocore.exceptions import ClientError

from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator

# --- Python Functions for Cloud Operations ---

# Define your S3 bucket name as a global constant so it's easy to change later.
BUCKET_NAME = "harry-genomeflow-data-lake-b33c1e0186c968cc"

def decompress_s3_file():
    """
    This function performs the first step of our cloud-native pipeline.
    It downloads the raw, gzipped FASTQ file from our S3 data lake,
    decompresses it in the Airflow worker's temporary storage,
    and then uploads the resulting uncompressed FASTQ file back to S3.
    """
    # Create a client to interact with S3.
    # Because we mounted our ~/.aws credentials, this will work automatically.
    s3_client = boto3.client('s3')

    # Define the "keys" (file paths) within our S3 bucket.
    input_key = "raw_reads/SRR062634.fastq.gz"
    output_key = "decompressed/SRR062634.fastq"

    # Define the temporary file paths inside the Airflow worker container.
    # The /tmp/ directory is a standard, temporary location on Linux systems.
    local_gz_file = "/tmp/SRR062634.fastq.gz"
    local_decompressed_file = "/tmp/SRR062634.fastq"

    # --- Step 1: Download the file from S3 ---
    print(f"Downloading s3://{BUCKET_NAME}/{input_key} to {local_gz_file}...")
    s3_client.download_file(BUCKET_NAME, input_key, local_gz_file)
    print("Download complete.")

    # --- Step 2: Decompress the local file using a subprocess ---
    # We call the 'gunzip' command-line tool that we installed in our Dockerfile.
    # The '-f' flag forces an overwrite, which is useful for re-running tasks.
    # The 'check=True' flag will make the task fail if gunzip returns an error.
    print(f"Decompressing {local_gz_file}...")
    subprocess.run(["gunzip", "-f", local_gz_file], check=True)
    print("Decompression complete.")

    # --- Step 3: Upload the decompressed result file back to S3 ---
    print(f"Uploading {local_decompressed_file} to s3://{BUCKET_NAME}/{output_key}...")
    s3_client.upload_file(local_decompressed_file, BUCKET_NAME, output_key)
    print("Upload complete.")

    # --- Step 4: Clean up temporary files (Good Practice) ---
    # This is not strictly necessary as the worker container is temporary,
    # but it's good practice to clean up after yourself.
    print("Cleaning up temporary local files...")
    subprocess.run(["rm", "-f", local_decompressed_file], check=True)
    print("Cleanup complete. Task finished.")



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
    dag_id="genomeflow_pipeline_v6",
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

    # Task 0: Removal of bash operator, redundant in new python refactor.

    # Task 1: Decompress the raw sequencing data from S3
    decompress_sample_cloud = PythonOperator(
    task_id="decompress_sample_from_s3",
    python_callable=decompress_s3_file,
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
    decompress_sample_cloud >> run_quality_control >> align_genome >> call_and_flag_variants

