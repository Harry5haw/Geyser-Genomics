# app/tasks.py (The definitive, final debug version)

import argparse
import boto3
import subprocess
import os
import threading
import time
from functools import wraps
import logging # <-- ADDED FOR DEBUGGING

# --- BOTO3 DEBUG LOGGING ---
# This is the most important change. It will show us the raw HTTP requests.
print("--- ENABLING BOTO3 DEBUG LOGGING ---")
boto3.set_stream_logger('botocore', level=logging.DEBUG)
# ------------------------------------

# --- Configuration ---
BUCKET_NAME = os.environ.get("BUCKET_NAME")
AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2") # Read region from env, default to eu-west-2

if not BUCKET_NAME:
    print("FATAL: BUCKET_NAME environment variable is not set.")
    exit(1)

# Initialize AWS clients with EXPLICIT region
s3_client = boto3.client('s3', region_name=AWS_REGION)
cloudwatch_client = boto3.client('cloudwatch', region_name=AWS_REGION)
METRIC_NAMESPACE = "TerraFlowGenomics"

# --- Decorator for Timing and Metrics ---
def time_task_and_emit_metric(task_name):
    """
    A decorator that times the execution of a function, prints the duration,
    and emits a 'Duration' metric to AWS CloudWatch.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            srr_id = args[0] if args else "UnknownSample"
            print(f"--- Starting task '{task_name}' for sample '{srr_id}' ---")
            start_time = time.time()
            
            try:
                result = func(*args, **kwargs)
                end_time = time.time()
                duration_seconds = end_time - start_time
                
                print(f"--- Task '{task_name}' for sample '{srr_id}' completed in {duration_seconds:.2f} seconds. ---")
                
                print("--- ATTEMPTING TO SEND CLOUDWATCH METRIC ---")
                cloudwatch_client.put_metric_data(
                    Namespace=METRIC_NAMESPACE,
                    MetricData=[
                        {
                            'MetricName': 'Duration',
                            'Dimensions': [
                                {'Name': 'TaskName', 'Value': task_name},
                                {'Name': 'SampleId', 'Value': srr_id},
                                {'Name': 'Status', 'Value': 'Success'}
                            ],
                            'Value': duration_seconds,
                            'Unit': 'Seconds'
                        },
                    ]
                )
                print("--- BOTO3 CALL COMPLETED WITHOUT EXCEPTION ---")
                return result
            except Exception as e:
                end_time = time.time()
                duration_seconds = end_time - start_time
                print(f"--- Task '{task_name}' for sample '{srr_id}' FAILED after {duration_seconds:.2f} seconds. Error: {e} ---")

                print("--- ATTEMPTING TO SEND FAILURE METRIC TO CLOUDWATCH ---")
                cloudwatch_client.put_metric_data(
                    Namespace=METRIC_NAMESPACE,
                    MetricData=[
                        {
                            'MetricName': 'Duration',
                            'Dimensions': [
                                {'Name': 'TaskName', 'Value': task_name},
                                {'Name': 'SampleId', 'Value': srr_id},
                                {'Name': 'Status', 'Value': 'Failure'}
                            ],
                            'Value': duration_seconds,
                            'Unit': 'Seconds'
                        },
                        {
                            'MetricName': 'FailureCount',
                            'Dimensions': [
                                {'Name': 'TaskName', 'Value': task_name},
                                {'Name': 'SampleId', 'Value': srr_id}
                            ],
                            'Value': 1,
                            'Unit': 'Count'
                        }
                    ]
                )
                print("--- BOTO3 FAILURE CALL COMPLETED WITHOUT EXCEPTION ---")
                # Re-raise the exception to ensure the Batch job is marked as failed
                raise

        return wrapper
    return decorator


# --- Bioinformatics Tasks (now decorated) ---

@time_task_and_emit_metric("Decompress")
def decompress_task(srr_id):
    """
    Downloads a compressed FASTQ from S3, decompresses it in-memory,
    and streams the uncompressed output back up to S3.
    """
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

@time_task_and_emit_metric("Align")
def align_task(srr_id, reference_name):
    """
    Downloads the FASTQ file and a specified reference genome, aligns them with BWA,
    and uploads the resulting BAM file to S3.
    """
    fastq_key = f"decompressed/{srr_id}.fastq"
    output_bam_key = f"alignments/{srr_id}.bam"
    local_fastq_path = f"/tmp/{srr_id}.fastq"
    local_ref_dir = "/tmp/reference/"
    local_ref_path = f"{local_ref_dir}{reference_name}"
    local_bam_path = f"/tmp/{srr_id}.bam"

    print(f"Downloading FASTQ file: {fastq_key}")
    s3_client.download_file(BUCKET_NAME, fastq_key, local_fastq_path)
    print("Downloading reference genome files...")
    s3_resource = boto3.resource('s3', region_name=AWS_REGION)
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

@time_task_and_emit_metric("QualityControl")
def qc_task(srr_id):
    """
    Downloads the decompressed FASTQ from S3, runs FastQC on it locally,
    and uploads the resulting reports back to S3.
    """
    input_key = f"decompressed/{srr_id}.fastq"
    local_fastq = f"/tmp/{srr_id}.fastq"
    local_qc_dir = "/tmp/qc_results/"

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

@time_task_and_emit_metric("CallVariants")
def variants_task(srr_id, reference_name):
    """
    Downloads the BAM file and a specified reference genome, calls variants with bcftools,
    and uploads the resulting VCF file to S3.
    """
    bam_key = f"alignments/{srr_id}.bam"
    output_vcf_key = f"variants/{srr_id}.vcf.gz"
    local_bam_path = f"/tmp/{srr_id}.bam"
    local_ref_dir = "/tmp/reference/"
    local_ref_path = f"{local_ref_dir}{reference_name}"

    print(f"Downloading BAM file: {bam_key}")
    s3_client.download_file(BUCKET_NAME, bam_key, local_bam_path)
    print("Downloading reference genome files...")
    s3_resource = boto3.resource('s3', region_name=AWS_REGION)
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


# --- Main execution block ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runs a bioinformatics pipeline task.")
    parser.add_argument("task_name", help="The name of the task to run: decompress, qc, align, variants")
    parser.add_argument("srr_id", help="The sample ID to process, e.g., SRR062634")
    parser.add_argument("reference_name", nargs="?", default=None, help="The reference genome filename. Required for align and variants.")
    args = parser.parse_args()

    task_map = {
        "decompress": decompress_task,
        "qc": qc_task,
        "align": align_task,
        "variants": variants_task
    }

    if args.task_name in task_map:
        if args.task_name in ["align", "variants"]:
            if not args.reference_name:
                print(f"Error: '{args.task_name}' task requires a reference_name argument.")
                exit(1)
            task_map[args.task_name](args.srr_id, args.reference_name)
        else:
            task_map[args.task_name](args.srr_id)
    else:
        print(f"Error: Unknown task '{args.task_name}'")
        exit(1)

    print(f"\nTask '{args.task_name}' driver script completed successfully for sample '{args.srr_id}'.")
