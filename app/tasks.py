# This is the complete, final, parameterized version of app/tasks.py

import argparse
import boto3
import subprocess
import os
import threading

# Read the bucket name from an environment variable set by AWS Batch.
BUCKET_NAME = os.environ.get("BUCKET_NAME")

if not BUCKET_NAME:
    print("FATAL: BUCKET_NAME environment variable is not set.")
    exit(1)


def decompress_task(srr_id):
    """
    Downloads a compressed FASTQ from S3, decompresses it in-memory,
    and streams the uncompressed output back up to S3.
    """
    s3_client = boto3.client('s3')
    input_key = f"raw_reads/{srr_id}.fastq.gz"
    output_key = f"decompressed/{srr_id}.fastq"

    print(f"Starting decompression stream for s3://{BUCKET_NAME}/{input_key}")
    s3_object = s3_client.get_object(Bucket=BUCKET_NAME, Key=input_key)
    streaming_body = s3_object['Body']
    gunzip_process = subprocess.Popen(["gunzip"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def upload_stream():
        try:
            s3_client.upload_fileobj(gunzip_process.stdout, BUCKET_NAME, output_key)
            print(f"Successfully decompressed and uploaded to s3://{BUCKET_NAME}/{output_key}")
        except Exception as e:
            print(f"Error during S3 upload: {e}")

    upload_thread = threading.Thread(target=upload_stream)
    upload_thread.start()
    
    try:
        for chunk in streaming_body.iter_chunks():
            gunzip_process.stdin.write(chunk)
        gunzip_process.stdin.close()
    except Exception as e:
        print(f"Error writing to gunzip process: {e}")
    
    upload_thread.join()
    return_code = gunzip_process.wait()
    if return_code != 0:
        error_output = gunzip_process.stderr.read().decode('utf-8')
        print(f"Gunzip process failed with return code {return_code}. Error: {error_output}")
        raise subprocess.CalledProcessError(return_code, gunzip_process.args, stderr=error_output)

def align_task(srr_id, reference_name):
    """
    Downloads the FASTQ file and a specified reference genome, aligns them with BWA,
    and uploads the resulting BAM file to S3.
    """
    s3_client = boto3.client('s3')
    fastq_key = f"decompressed/{srr_id}.fastq"
    output_bam_key = f"alignments/{srr_id}.bam"
    local_fastq_path = f"/tmp/{srr_id}.fastq"
    local_ref_dir = "/tmp/reference/"
    local_ref_path = f"{local_ref_dir}{reference_name}" # Use the parameter
    local_bam_path = f"/tmp/{srr_id}.bam"

    print(f"Downloading FASTQ file: {fastq_key}")
    s3_client.download_file(BUCKET_NAME, fastq_key, local_fastq_path)
    print("Downloading reference genome files...")
    s3_resource = boto3.resource('s3')
    bucket = s3_resource.Bucket(BUCKET_NAME)
    os.makedirs(local_ref_dir, exist_ok=True)
    for obj in bucket.objects.filter(Prefix="reference/"):
        target = os.path.join(local_ref_dir, os.path.basename(obj.key))
        if not os.path.exists(os.path.dirname(target)):
            os.makedirs(os.path.dirname(target))
        bucket.download_file(obj.key, target)
    print("All downloads complete.")
    print(f"Running BWA-MEM alignment for {srr_id}...")
    alignment_command = (
        f"bwa mem {local_ref_path} {local_fastq_path} | "
        f"samtools view -S -b > {local_bam_path}"
    )
    subprocess.run(alignment_command, shell=True, check=True)
    print("Alignment complete.")
    print(f"Uploading BAM file to s3://{BUCKET_NAME}/{output_bam_key}")
    s3_client.upload_file(local_bam_path, BUCKET_NAME, output_bam_key)
    print("Upload complete.")
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
    local_fastq = f"/tmp/{srr_id}.fastq"
    local_qc_dir = "/tmp/qc_results/"

    print(f"Starting QC task for {srr_id}")
    print(f"Downloading s3://{BUCKET_NAME}/{input_key} to {local_fastq}")
    s3_client.download_file(BUCKET_NAME, input_key, local_fastq)
    print("Download complete.")
    os.makedirs(local_qc_dir, exist_ok=True)
    print(f"Running FastQC on {local_fastq}...")
    fastqc_command = ["fastqc", local_fastq, "-o", local_qc_dir]
    subprocess.run(fastqc_command, check=True)
    print("FastQC analysis complete.")
    output_html_local = f"{local_qc_dir}{srr_id}_fastqc.html"
    output_zip_local = f"{local_qc_dir}{srr_id}_fastqc.zip"
    output_html_s3_key = f"qc_reports/{srr_id}_fastqc.html"
    output_zip_s3_key = f"qc_reports/{srr_id}_fastqc.zip"
    print(f"Uploading HTML report to s3://{BUCKET_NAME}/{output_html_s3_key}")
    s3_client.upload_file(output_html_local, BUCKET_NAME, output_html_s3_key)
    print(f"Uploading ZIP archive to s3://{BUCKET_NAME}/{output_zip_s3_key}")
    s3_client.upload_file(output_zip_local, BUCKET_NAME, output_zip_s3_key)
    print("Report uploads complete.")
    print("Cleaning up temporary files...")
    subprocess.run(["rm", "-rf", local_fastq, local_qc_dir], check=True)
    print("QC task complete.")

def variants_task(srr_id, reference_name):
    """
    Downloads the BAM file and a specified reference genome, calls variants with bcftools,
    and uploads the resulting VCF file to S3.
    """
    s3_client = boto3.client('s3')
    bam_key = f"alignments/{srr_id}.bam"
    output_vcf_key = f"variants/{srr_id}.vcf.gz"
    local_bam_path = f"/tmp/{srr_id}.bam"
    local_ref_dir = "/tmp/reference/"
    local_ref_path = f"{local_ref_dir}{reference_name}" # Use the parameter

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
    print(f"Calling variants for {srr_id}...")
    variant_calling_command = (
        f"bcftools mpileup -f {local_ref_path} {local_bam_path} | "
        f"bcftools call -mv -o - -O z > /tmp/{srr_id}.vcf.gz"
    )
    subprocess.run(variant_calling_command, shell=True, check=True)
    print("Variant calling complete.")
    local_vcf_path = f"/tmp/{srr_id}.vcf.gz"
    print(f"Uploading VCF file to s3://{BUCKET_NAME}/{output_vcf_key}")
    s3_client.upload_file(local_vcf_path, BUCKET_NAME, output_vcf_key)
    print("Upload complete.")
    print("Cleaning up temporary local files...")
    subprocess.run(["rm", "-rf", local_bam_path, local_ref_dir, local_vcf_path], check=True)
    print(f"s3://{BUCKET_NAME}/{output_vcf_key}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runs a bioinformatics pipeline task.")
    parser.add_argument("task_name", help="The name of the task to run: decompress, qc, align, variants")
    parser.add_argument("srr_id", help="The sample ID to process, e.g., SRR062634")
    parser.add_argument("reference_name", nargs="?", default=None, help="The reference genome filename (e.g., chr20.fa). Required for align and variants.")
    args = parser.parse_args()

    if args.task_name == "decompress":
        decompress_task(args.srr_id)
    elif args.task_name == "qc":
        qc_task(args.srr_id)
    elif args.task_name == "align":
        if not args.reference_name:
            print("Error: 'align' task requires a reference_name argument.")
            exit(1)
        align_task(args.srr_id, args.reference_name)
    elif args.task_name == "variants":
        if not args.reference_name:
            print("Error: 'variants' task requires a reference_name argument.")
            exit(1)
        variants_task(args.srr_id, args.reference_name)
    else:
        print(f"Error: Unknown task '{args.task_name}'")
        exit(1)

    print(f"Task '{args.task_name}' completed successfully for sample '{args.srr_id}'.")
