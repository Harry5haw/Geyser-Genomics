#!/usr/bin/env bash
# Strict commit → issue linking (idempotent, Windows-friendly)
# Requires: gh CLI and jq

MAPPING_FILE="mapping.json"

if ! command -v gh &> /dev/null; then
    echo "❌ GitHub CLI not installed."
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "❌ jq not installed."
    exit 1
fi

if [ ! -f "$MAPPING_FILE" ]; then
    echo "❌ mapping.json not found!"
    exit 1
fi

echo "🚀 Linking commits to issues (strict mode, idempotent)..."

jq -r 'to_entries[] | "\(.key) \(.value)"' "$MAPPING_FILE" | while read -r sha issue; do
    issue=$(echo "$issue" | tr -d '\r')   # strip CR from Windows Git Bash

    if git cat-file -e "$sha" 2>/dev/null; then
        message=$(git log -1 --pretty=format:"%s" "$sha")
        echo "🔗 Checking commit $sha → Issue #$issue"

        # Check if the commit SHA already exists in issue comments
        if gh issue view "$issue" --comments | grep -q "$sha"; then
            echo "⏭️  Commit $sha already linked to Issue #$issue, skipping..."
        else
            echo "✅ Linking commit $sha → Issue #$issue"
            gh issue comment "$issue" --body "Linked commit: $sha — $message"
        fi
    else
        echo "⚠️ Commit $sha not found in this repo"
    fi
done

echo "🎉 Done! All commits processed."
