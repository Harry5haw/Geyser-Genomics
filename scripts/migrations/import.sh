#!/usr/bin/env bash

# Bulk import GitHub Issues from a JSON file using gh CLI
# Requires: gh CLI (https://cli.github.com/) and jq (https://stedolan.github.io/jq/)

INPUT_FILE="issues.json"

if ! command -v gh &> /dev/null; then
    echo "❌ GitHub CLI (gh) not installed. Install from https://cli.github.com/"
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "❌ jq not installed. Install from https://stedolan.github.io/jq/"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "❌ issues.json not found in current directory."
    exit 1
fi

echo "🚀 Importing issues from $INPUT_FILE..."

jq -c '.[]' "$INPUT_FILE" | while read -r issue; do
    title=$(echo "$issue" | jq -r .title)
    body=$(echo "$issue" | jq -r .body)
    labels=$(echo "$issue" | jq -r '.labels | join(",")')

    echo "📌 Creating issue: $title"
    gh issue create --title "$title" --body "$body" --label "$labels"
done

echo "✅ All issues imported!"
