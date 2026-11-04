# RP-2 extraction scripts

The data behind the RP-2 study: an empirical study of AI-agent-directed software engineering on this project
(the plan: what went wrong, what caught it, what the human kept, what the guardrails cost).

Every script is **read-only** on its sources and writes CSVs only to a folder **outside every git repository**.
The scripts refuse anything else, so mined data is never committed. Standard library only, plus `git` and `gh`.

| Script | Reads | Writes |
| :--- | :--- | :--- |
| `mine_prs.py` | `gh pr list`, then `gh pr view` for each PR's commits | `prs.csv`: class (infra/offline), areas, size, cycle time, agent-written, issue refs, plan counts in the body, labels |
| `mine_commits.py` | `git log --numstat` on a ref (default `origin/main`) | `commits.csv`: merges, reverts, agent co-authored, the merged PR, lines per area |
| `mine_ci.py` | `gh run list` (and `gh run view` for failed runs, unless `--no-jobs`) | `ci_runs.csv`, `ci_failed_jobs.csv` |
| `mine_issues.py` | `gh issue list` | `issues.csv`: labels, open time, refs |
| `mine_decisions.py` | the dissertation's `notes/decisions.md` | `decisions.csv`: the "Who decided" text verbatim, plus a first-pass class |
| `mine_cloud_runs.py` | the dissertation's `data/cloud_runs.csv` | `cloud_runs_by_execution.csv`: one row per execution, its purpose, cited issues |
| `redact_transcripts.py` | Claude Code session transcripts (**run by the user only**) | `events.jsonl`, `summary.json`, `spot_check.txt`: allow-listed events, redacted |

    OUT=~/Documents/Projects/geyser-data/rp2/data
    python scripts/rp2/mine_prs.py --out $OUT
    python scripts/rp2/mine_commits.py --out $OUT
    python scripts/rp2/mine_issues.py --out $OUT
    python scripts/rp2/mine_ci.py --out $OUT
    python scripts/rp2/mine_decisions.py --notes ../geyser-dissertation/notes/decisions.md --out $OUT
    python scripts/rp2/mine_cloud_runs.py --runs ../geyser-dissertation/data/cloud_runs.csv --out $OUT

## Transcripts: the user runs the redaction, then spot-checks it

Session transcripts can hold secrets and personal data, so no agent reads them.
1. The user runs `redact_transcripts.py` into a **new** folder outside every repo.
2. The user reads `spot_check.txt` (20 random output lines) and, if needed, `events.jsonl`, before anyone else does.
3. Only the redacted `events.jsonl` is analysed.

What survives is an allow-list: tool names, times, error/denial flags and classes, the redacted first 300 characters
of an error or denial, the redacted first line of a denied command, and peer-message senders and first lines.
Prompt and reply text is never copied, only counted. Session IDs are hashed. Keys, tokens, account IDs, ARNs,
e-mail addresses, IPs, presigned queries, home paths and the names given with `--name` are redacted.
