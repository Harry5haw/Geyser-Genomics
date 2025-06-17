# app/tasks.py
import argparse
import boto3
import subprocess
import os

# Define the S3 bucket as a global constant
BUCKET_NAME = "harry-genomeflow-data-lake-b33c1e0186c968cc"


# Python Functions app/tasks.py

def decompress_task(srr_id):
    """
    Downloads a compressed FASTQ from S3, decompresses it in-memory,
    and streams the uncompressed output back up to S3.
    This is a memory-efficient streaming operation.
    """
    s3_client = boto3.client('s3')
    input_key = f"raw_reads/{srr_id}.fastq.gz"
    output_key = f"decompressed/{srr_id}.fastq"

    print(f"Starting decompression stream for s3://{BUCKET_NAME}/{input_key}")

    # Create boto3 streaming objects
    s3_object = s3_client.get_object(Bucket=BUCKET_NAME, Key=input_key)
    streaming_body = s3_object['Body']

    # Set up the piped subprocess command
    # gunzip reads from stdin, decompresses, and writes to stdout
    gunzip_process = subprocess.Popen(["gunzip"], stdin=streaming_body, stdout=subprocess.PIPE)
    
    # Upload the output of the gunzip stream directly to S3
    # This avoids saving the large uncompressed file to disk.
    s3_client.upload_fileobj(gunzip_process.stdout, BUCKET_NAME, output_key)
    
    print(f"Successfully decompressed and uploaded to s3://{BUCKET_NAME}/{output_key}")

def align_task(srr_id):
    """
    Downloads the FASTQ file and reference genome, aligns them with BWA,
    and uploads the resulting BAM file to S3.
    """
    s3_client = boto3.client('s3')
    
    # Define S3 keys and local paths
    fastq_key = f"decompressed/{srr_id}.fastq"
    output_bam_key = f"alignments/{srr_id}.bam"

    local_fastq_path = f"/tmp/{srr_id}.fastq"
    local_ref_dir = "/tmp/reference/"
    local_ref_path = f"{local_ref_dir}reference.fa" # BWA needs the path to the .fa file
    local_bam_path = f"/tmp/{srr_id}.bam"
    
    # --- Step 1: Download all necessary input files ---
    print(f"Downloading FASTQ file: {fastq_key}")
    s3_client.download_file(BUCKET_NAME, fastq_key, local_fastq_path)

    print("Downloading reference genome files...")
    # This pattern downloads all files from the 'reference/' prefix in S3
    s3_resource = boto3.resource('s3')
    bucket = s3_resource.Bucket(BUCKET_NAME)
    os.makedirs(local_ref_dir, exist_ok=True)
    for obj in bucket.objects.filter(Prefix="reference/"):
        target = os.path.join(local_ref_dir, os.path.basename(obj.key))
        if not os.path.exists(os.path.dirname(target)):
            os.makedirs(os.path.dirname(target))
        bucket.download_file(obj.key, target)
    print("All downloads complete.")
    
    # --- Step 2: Run the BWA-MEM and Samtools piped command ---
    print(f"Running BWA-MEM alignment for {srr_id}...")
    
    # This command is a single string that will be run with shell=True
    # It streams the output of 'bwa mem' directly into 'samtools view'
    # This is efficient as the huge SAM text output is never written to disk.
    alignment_command = (
        f"bwa mem {local_ref_path} {local_fastq_path} | "
        f"samtools view -S -b > {local_bam_path}"
    )
    
    subprocess.run(alignment_command, shell=True, check=True)
    print("Alignment complete.")

    # --- Step 3: Upload the final BAM file to S3 ---
    print(f"Uploading BAM file to s3://{BUCKET_NAME}/{output_bam_key}")
    s3_client.upload_file(local_bam_path, BUCKET_NAME, output_bam_key)
    print("Upload complete.")

    # --- Step 4: Clean up ---
    print("Cleaning up temporary local files...")
    subprocess.run(["rm", "-rf", local_fastq_path, local_ref_dir, local_bam_path], check=True)
    print("Alignment task complete.")

def qc_task(srr_id):
    """
    Downloads the decompressed FASTQ from S3, runs FastQC on it locally,
    and uploads the resulting reports back to S3.
    """
    s3_client = boto3.client('s3')
    input_key = f"decompressed/{srr_id}.fastq"

    # Define temporary local paths inside the container
    local_fastq = f"/tmp/{srr_id}.fastq"
    local_qc_dir = "/tmp/qc_results/"

    print(f"Starting QC task for {srr_id}")

    # Step 1: Download the uncompressed FASTQ file from S3.
    # We have to download this one, as FastQC needs a file on disk to operate on.
    print(f"Downloading s3://{BUCKET_NAME}/{input_key} to {local_fastq}")
    s3_client.download_file(BUCKET_NAME, input_key, local_fastq)
    print("Download complete.")

    # Step 2: Run FastQC on the local file
    os.makedirs(local_qc_dir, exist_ok=True) # Ensure the output dir exists
    print(f"Running FastQC on {local_fastq}...")
    fastqc_command = ["fastqc", local_fastq, "-o", local_qc_dir]
    subprocess.run(fastqc_command, check=True)
    print("FastQC analysis complete.")

    # Step 3: Upload the two small report files to S3
    output_html_local = f"{local_qc_dir}{srr_id}_fastqc.html"
    output_zip_local = f"{local_qc_dir}{srr_id}_fastqc.zip"
    
    output_html_s3_key = f"qc_reports/{srr_id}_fastqc.html"
    output_zip_s3_key = f"qc_reports/{srr_id}_fastqc.zip"

    print(f"Uploading HTML report to s3://{BUCKET_NAME}/{output_html_s3_key}")
    s3_client.upload_file(output_html_local, BUCKET_NAME, output_html_s3_key)

    print(f"Uploading ZIP archive to s3://{BUCKET_NAME}/{output_zip_s3_key}")
    s3_client.upload_file(output_zip_local, BUCKET_NAME, output_zip_s3_key)
    print("Report uploads complete.")

    # Step 4: Clean up all temporary files
    print("Cleaning up temporary files...")
    subprocess.run(["rm", "-rf", local_fastq, local_qc_dir], check=True)
    print("QC task complete.")

def variants_task(srr_id):
    """
    Downloads the BAM file and reference genome, calls variants with bcftools,
    and uploads the resulting VCF file to S3.
    """
    s3_client = boto3.client('s3')
    
    # Define S3 keys and local paths
    bam_key = f"alignments/{srr_id}.bam"
    ref_genome_key = "reference/reference.fa" # bcftools needs the reference
    output_vcf_key = f"variants/{srr_id}.vcf.gz" # VCFs are text and compress well

    local_bam_path = f"/tmp/{srr_id}.bam"
    local_ref_dir = "/tmp/reference/"
    local_ref_path = f"{local_ref_dir}reference.fa"
    
    # --- Step 1: Download all necessary input files ---
    print(f"Downloading BAM file: {bam_key}")
    s3_client.download_file(BUCKET_NAME, bam_key, local_bam_path)

    print("Downloading reference genome files...")
    s3_resource = boto3.resource('s3')
    bucket = s3_resource.Bucket(BUCKET_NAME)
    os.makedirs(local_ref_dir, exist_ok=True)
    for obj in bucket.objects.filter(Prefix="reference/"):
        target = os.path.join(local_ref_dir, os.path.basename(obj.key))
        bucket.download_file(obj.key, target)
    print("All downloads complete.")

    # --- Step 2: Run the bcftools piped command ---
    print(f"Calling variants for {srr_id}...")

    # This command streams the output of 'bcftools mpileup' directly into 'bcftools call'.
    # '-f' specifies the reference genome.
    # '-o -' tells 'call' to write to standard output.
    # '-O z' tells 'call' to output in compressed VCF format (.vcf.gz)
    variant_calling_command = (
        f"bcftools mpileup -f {local_ref_path} {local_bam_path} | "
        f"bcftools call -mv -o - -O z > /tmp/{srr_id}.vcf.gz"
    )
    
    subprocess.run(variant_calling_command, shell=True, check=True)
    print("Variant calling complete.")

    # --- Step 3: Upload the final VCF file to S3 ---
    local_vcf_path = f"/tmp/{srr_id}.vcf.gz"
    print(f"Uploading VCF file to s3://{BUCKET_NAME}/{output_vcf_key}")
    s3_client.upload_file(local_vcf_path, BUCKET_NAME, output_vcf_key)
    print("Upload complete.")

    # --- Step 4: Clean up ---
    print("Cleaning up temporary local files...")
    subprocess.run(["rm", "-rf", local_bam_path, local_ref_dir, local_vcf_path], check=True)
    print("Variant calling task complete.")


# --- Command-Line Interface Logic ---
# This block runs when the script is executed from the command line
if __name__ == "__main__":
    # Set up an argument parser to read command-line arguments
    parser = argparse.ArgumentParser(description="Runs a bioinformatics pipeline task.")
    parser.add_argument("task_name", help="The name of the task to run: decompress, qc, align, variants")
    parser.add_argument("srr_id", help="The sample ID to process, e.g., SRR062634")
    
    args = parser.parse_args()

    # Call the appropriate function based on the provided task_name
    if args.task_name == "decompress":
        decompress_task(args.srr_id)
    # We will add elif blocks for our other tasks here
    elif args.task_name == "qc":  
        qc_task(args.srr_id)
    elif args.task_name == "align": 
        align_task(args.srr_id)
    elif args.task_name == "variants": # <-- ADD THIS FINAL BLOCK
        variants_task(args.srr_id)
    else:
        print(f"Error: Unknown task '{args.task_name}'")
        exit(1) # Exit with a non-zero code to indicate failure

    print(f"Task '{args.task_name}' completed successfully for sample '{args.srr_id}'.")
