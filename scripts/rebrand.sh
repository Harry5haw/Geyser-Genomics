#!/usr/bin/env bash
set -euo pipefail

# Map old → new
declare -A RENAME_MAP=(
  ["GeyserGenomics"]="GeyserGenomics"
  ["geyser-dashboard-dev"]="geyser-dashboard-dev"
  ["geyser-pipeline-runtime"]="geyser-pipeline-runtime"
  ["Geyser Genomics"]="Geyser Genomics"
)

# Find all files except .venv, .terraform, .git
find . -type f \
  ! -path "*/.venv/*" \
  ! -path "*/.terraform/*" \
  ! -path "*/.git/*" \
| while read -r file; do
  for old in "${!RENAME_MAP[@]}"; do
    new=${RENAME_MAP[$old]}
    if grep -q "$old" "$file"; then
      echo "Updating $old → $new in $file"
      sed -i "s/$old/$new/g" "$file"
    fi
  done
done
