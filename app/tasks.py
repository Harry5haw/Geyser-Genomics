# app/tasks.py
import argparse
import boto3
import subprocess
import os

# Define the S3 bucket as a global constant
BUCKET_NAME = "harry-genomeflow-data-lake-b33c1e0186c968cc"

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
    else:
        print(f"Error: Unknown task '{args.task_name}'")
        exit(1) # Exit with a non-zero code to indicate failure

    print(f"Task '{args.task_name}' completed successfully for sample '{args.srr_id}'.")

