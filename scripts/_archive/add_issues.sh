#!/usr/bin/env bash

# Add issues 6 through 23 to Project #2
for issue in $(seq 6 23); do
  echo "➕ Adding issue #$issue to Project 2..."
  gh projects item-add 2 --user "@me" \
    --url "https://github.com/Harry5haw/Geyser-Genomics/issues/$issue"
done
