#!/bin/bash
set -e -o pipefail

# --- Logging Functions ---
log_info() { echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") [INFO] ==> $1"; }
log_error() { echo >&2 "$(date -u +"%Y-%m-%dT%H:%M:%SZ") [ERROR] ==> $1"; }

# --- Error Handling ---
error_handler() {
  log_error "Script failed on line $2 with exit code $1. Failing command: '$3'"
  sleep 1
}
trap 'error_handler $? $LINENO "$BASH_COMMAND"' ERR

# --- Main Script Logic ---
main() {
  log_info "DeepVariant job started."
  : "${SAMPLE_ID:?FATAL: SAMPLE_ID must be set}"
  : "${S3_BUCKET:?FATAL: S3_BUCKET must be set}"
  : "${REF_KEY:?FATAL: REF_KEY must be set}"
  : "${BAM_KEY:?FATAL: BAM_KEY must be set}"
  : "${MODEL_TYPE:?FATAL: MODEL_TYPE must be set}"
  
  LOCAL_REF="/work/reference.fasta"
  LOCAL_BAM="/work/input.bam"
  LOCAL_OUTDIR="/work/output"
  mkdir -p "${LOCAL_OUTDIR}"

  # --- CORRECTED: Use 'cp' for local paths, 'aws s3 cp' for S3 paths ---
  log_info "Downloading inputs..."
  if [[ "${S3_BUCKET}" == /* ]]; then
    log_info "Local path detected. Using 'cp' for download simulation."
    cp "${S3_BUCKET}/${REF_KEY}" "${LOCAL_REF}"
    cp "${S3_BUCKET}/${BAM_KEY}" "${LOCAL_BAM}"
    cp "${S3_BUCKET}/${BAM_KEY}.bai" "${LOCAL_BAM}.bai"
  else
    log_info "S3 bucket detected. Using 'aws s3 cp' for download."
    aws s3 cp "s3://${S3_BUCKET}/${REF_KEY}" "${LOCAL_REF}"
    aws s3 cp "s3://${S3_BUCKET}/${BAM_KEY}" "${LOCAL_BAM}"
    aws s3 cp "s3://${S3_BUCKET}/${BAM_KEY}.bai" "${LOCAL_BAM}.bai"
  fi
  log_info "Download complete."

  log_info "Checking for reference index (.fai)..."
  if [[ ! -f "${LOCAL_REF}.fai" ]]; then
    log_info "Generating samtools faidx..."
    samtools faidx "${LOCAL_REF}"
  fi

  log_info "Starting DeepVariant process..."
  /opt/deepvariant/bin/run_deepvariant \
    --model_type="${MODEL_TYPE}" \
    --ref="${LOCAL_REF}" \
    --reads="${LOCAL_BAM}" \
    --output_vcf="${LOCAL_OUTDIR}/${SAMPLE_ID}.deepvariant.vcf.gz" \
    --output_gvcf="${LOCAL_OUTDIR}/${SAMPLE_ID}.deepvariant.g.vcf.gz" \
    --num_shards="$(nproc)"
  log_info "DeepVariant finished successfully."

  VCF_OUTPUT="${LOCAL_OUTDIR}/${SAMPLE_ID}.deepvariant.vcf.gz"
  if [[ ! -s "${VCF_OUTPUT}" ]]; then
    log_error "FATAL: Output VCF not created or empty."
    exit 1
  fi

  # --- CORRECTED: Use 'cp' for local paths, 'aws s3 sync' for S3 paths ---
  log_info "Uploading results..."
  if [[ "${S3_BUCKET}" == /* ]]; then
    log_info "Local path detected. Using 'cp' for upload simulation."
    # The destination is the 'output' folder we mounted, inside a new structure.
    DEST_DIR="${S3_BUCKET}/outputs/variants/${SAMPLE_ID}/"
    mkdir -p "${DEST_DIR}"
    cp -r ${LOCAL_OUTDIR}/* "${DEST_DIR}"
  else
    log_info "S3 bucket detected. Using 'aws s3 sync' for upload."
    DEST_PATH="s3://${S3_BUCKET}/outputs/variants/${SAMPLE_ID}/"
    aws s3 sync "${LOCAL_OUTDIR}" "${DEST_PATH}"
  fi
  log_info "Upload complete."

  log_info "DeepVariant job completed successfully."
}

main
