"""RP-2 extraction scripts (scripts/rp2/): synthetic inputs only. No real transcript, gh call or git history is read."""

import csv
import json
import os
import sys

import pytest

from conftest import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "rp2"))
import mine_ci  # noqa: E402
import mine_cloud_runs  # noqa: E402
import mine_commits  # noqa: E402
import mine_decisions  # noqa: E402
import mine_issues  # noqa: E402
import mine_prs  # noqa: E402
import redact_transcripts as rt  # noqa: E402
import rp2_common  # noqa: E402


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- common ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,area",
    [
        ("infrastructure/step_functions.tf", "infra"),
        (".github/workflows/deploy-platform.yml", "infra"),
        (".github/workflows/tests.yml", "ci"),
        ("app/tasks.py", "app"),
        ("lambda/trigger/index.py", "app"),
        ("gate/src/main.rs", "gate"),
        ("web/samples/views.py", "web"),
        ("tests/test_rp2.py", "tests"),
        ("scripts/rp2/mine_prs.py", "scripts"),
        ("docs/operations.md", "docs"),
        ("README.md", "docs"),
        ("Dockerfile", "other"),
    ],
)
def test_area_of(path, area):
    assert rp2_common.area_of(path) == area


def test_outputs_are_refused_inside_a_git_repository(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit, match="inside a git repository"):
        rp2_common.out_dir(str(tmp_path / "repo" / "data"))
    assert rp2_common.out_dir(str(tmp_path / "outside")) == str(tmp_path / "outside")
    with pytest.raises(SystemExit):
        rp2_common.out_dir(os.path.join(REPO_ROOT, "rp2-out"))  # this repository itself


def test_hours_between():
    assert rp2_common.hours_between("2026-11-03T10:00:00Z", "2026-11-03T13:30:00Z") == 3.5
    assert rp2_common.hours_between("2026-11-03T10:00:00Z", None) is None


# --- PRs, commits, CI, issues -----------------------------------------------------------------------------------

PR = {
    "number": 192,
    "title": "Per-run overrides",
    "state": "MERGED",
    "isDraft": False,
    "createdAt": "2026-11-03T20:00:00Z",
    "mergedAt": "2026-11-03T23:30:00Z",
    "closedAt": "2026-11-03T23:30:00Z",
    "headRefName": "infra/run-overrides",
    "baseRefName": "main",
    "additions": 900,
    "deletions": 40,
    "files": [{"path": "infrastructure/state_machine.asl.json"}, {"path": "tests/test_consistency.py"}],
    "labels": [{"name": "infrastructure"}],
    "body": "Carries #190 and #195. **`Plan: 0 to add, 1 to change, 0 to destroy.`** Refs #189, #192.\n\n"
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    "commits": [
        {"messageHeadline": "Overrides", "messageBody": "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"},
        {"messageHeadline": "Merge main", "messageBody": ""},
    ],
}


def test_a_pr_row():
    row = mine_prs.pr_row(PR)
    assert row["class"] == "infra" and row["areas"] == ["infra", "tests"] and row["cycle_hours"] == 3.5
    assert row["agent_written"] and row["agent_commits"] == 1 and row["commits"] == 2
    assert row["issue_refs"] == [189, 190, 195]  # not itself
    assert row["plan_counts"] == ["0/1/0"] and row["labels"] == ["infrastructure"]


def test_mine_prs_writes_the_csv(tmp_path, monkeypatch):
    calls = []
    listed = {k: v for k, v in PR.items() if k != "commits"}

    def fake(args, cwd=None):
        calls.append(args)
        return [dict(listed)] if args[:2] == ["pr", "list"] else {"commits": PR["commits"]}

    monkeypatch.setattr(rp2_common, "gh_json", fake)
    assert mine_prs.main(["--out", str(tmp_path)]) == 0
    assert "commits" not in calls[0][-1].split(",") and calls[1] == ["pr", "view", "192", "--json", "commits"]
    (row,) = read_csv(tmp_path / "prs.csv")
    assert row["number"] == "192" and row["areas"] == "infra;tests" and row["agent_written"] == "1"
    assert row["commits"] == "2" and row["agent_commits"] == "1"
    assert calls[0][:3] == ["pr", "list", "--state"]


def log_record(sha, parents, subject, body="", numstat=""):
    """One commit as `git log --format=mine_commits.FORMAT --numstat` prints it."""
    when = "2026-11-03T20:00:00+00:00"
    fields = [sha, parents, "A. Author", when, when, subject, body]
    return mine_commits.START + mine_commits.SEP.join(fields) + mine_commits.SEP + "\n" + numstat


def test_commits_are_parsed_with_lines_per_area():
    numstat = (
        "10\t2\tinfrastructure/step_functions.tf\n5\t0\ttests/{old.py => test_consistency.py}\n-\t-\tdocs/figure.png\n"
    )
    text = (
        log_record("a1", "p1 p2", "Merge pull request #192 from x")
        + log_record("b2", "p3", "Overrides", "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", numstat)
        + log_record("c3", "p4", 'Revert "Overrides"')
    )
    merge, work, revert = mine_commits.parse_log(text)
    assert merge["merge"] and merge["pr"] == "192" and merge["files"] == 0
    assert work["agent"] and not work["merge"] and work["files"] == 3 and (work["added"], work["deleted"]) == (15, 2)
    assert work["added_infra"] == 10 and work["deleted_infra"] == 2 and work["added_tests"] == 5
    assert revert["revert"] and not revert["agent"]


def test_ci_runs_and_failed_jobs(tmp_path, monkeypatch):
    runs = [
        {
            "databaseId": 1,
            "workflowName": "Tests",
            "event": "pull_request",
            "headBranch": "b",
            "headSha": "s",
            "status": "completed",
            "conclusion": "failure",
            "createdAt": "2026-11-03T10:00:00Z",
            "updatedAt": "2026-11-03T10:06:00Z",
            "attempt": 1,
        },
        {
            "databaseId": 2,
            "workflowName": "Tests",
            "event": "push",
            "headBranch": "main",
            "headSha": "t",
            "status": "completed",
            "conclusion": "success",
            "createdAt": "2026-11-03T11:00:00Z",
            "updatedAt": "2026-11-03T11:04:00Z",
            "attempt": 1,
        },
    ]
    jobs = {
        "jobs": [
            {"name": "Unit tests", "conclusion": "failure", "steps": [{"name": "Run pytest", "conclusion": "failure"}]},
            {"name": "Lint", "conclusion": "success", "steps": []},
        ]
    }

    def fake(args, cwd=None):
        return runs if args[:2] == ["run", "list"] else jobs

    monkeypatch.setattr(rp2_common, "gh_json", fake)
    assert mine_ci.main(["--out", str(tmp_path)]) == 0
    assert [r["minutes"] for r in read_csv(tmp_path / "ci_runs.csv")] == ["6.0", "4.0"]
    (job,) = read_csv(tmp_path / "ci_failed_jobs.csv")
    assert job["run"] == "1" and job["job"] == "Unit tests" and job["failed_steps"] == "Run pytest"


def test_an_issue_row():
    row = mine_issues.issue_row(
        {
            "number": 200,
            "title": "Flaky",
            "state": "CLOSED",
            "stateReason": "COMPLETED",
            "createdAt": "2026-11-03T22:00:00Z",
            "closedAt": "2026-11-04T01:00:00Z",
            "labels": [{"name": "bug"}, {"name": "Testing"}],
            "milestone": {"title": "v2.0 Architecture"},
            "body": "Fixed by #202, see #200 and #204",
        }
    )
    assert row["open_hours"] == 3.0 and row["labels"] == ["bug", "Testing"] and row["refs"] == [202, 204]


# --- dissertation notes ------------------------------------------------------------------------------------------

DECISIONS = """# Decisions

## Index
| # | Date | Decision | Source |
| ---: | :--- | :--- | :--- |
| 1 | 2025-06 | Stateless Batch jobs | `changelog.md` |
| 59 | 2026-11-03 | Fail mode applied, then rolled back | #157 |
| P1 | open | State locking | #135, #163 |

---

## Decisions

### 1. Stateless Batch jobs
- **Date:** 2025-06
- **Who decided:** user/maintainer.

### 59. Fail mode applied, then rolled back
- **Who decided:** user/maintainer (rollback rule), with Claude proposing the smoke profile.

### P1. State locking
- **Who decided:** Claude (offline PR), from the 2.0 plan.
"""


def test_decisions_are_parsed_with_who_decided():
    rows = {r["id"]: r for r in mine_decisions.parse(DECISIONS)}
    assert list(rows) == ["1", "59", "P1"]
    assert rows["59"]["issue_refs"] == [157] and rows["P1"]["issue_refs"] == [135, 163]
    assert rows["1"]["who_class"] == "user" and rows["59"]["who_class"] == "user+claude"
    assert rows["P1"]["who_class"] == "claude-offline" and rows["59"]["who_raw"].startswith("user/maintainer (rollback")


@pytest.mark.parametrize(
    "text,cls",
    [
        ("user/maintainer.", "user"),
        ("user/maintainer, with Claude (planning session)", "user+claude"),
        ("Claude, approved by user", "claude-approved"),
        ("Claude (offline PR), from the 2.0 plan", "claude-offline"),
        ("not recorded in the source", "unknown"),
    ],
)
def test_who_class(text, cls):
    assert mine_decisions.who_class(text) == cls


def test_cloud_runs_fold_into_executions(tmp_path):
    runs = tmp_path / "cloud_runs.csv"
    runs.write_text(
        "execution,date,result,sample,reads,stage,entered_utc,exited_utc,seconds,image,notes\n"
        "CLOUD001-x,2026-11-02,FAILED,CLOUD001,10000,decompress,09:47,09:48,55.3,dev-local,io.UnsupportedOperation: seek (#71)\n"
        "REAL-HG003-35x-y,2026-11-02,SUCCEEDED,REAL-HG003-35x,1,qc,1,2,60,main-x,\n"
        "REAL-HG003-35x-y,2026-11-02,SUCCEEDED,REAL-HG003-35x,1,align,1,2,600.5,main-x,\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert mine_cloud_runs.main(["--runs", str(runs), "--out", str(out)]) == 0
    first, second = read_csv(out / "cloud_runs_by_execution.csv")
    assert first["purpose"] == "check run" and first["refs"] == "71" and first["result"] == "FAILED"
    assert second["purpose"] == "real sample" and second["stages"] == "qc;align" and second["stage_seconds"] == "660.5"


# --- transcript redaction -----------------------------------------------------------------------------------------

# Made-up secrets: none is real. Each must vanish from every output file.
SECRETS = [
    "AKIAABCDEFGHIJKLMNOP",
    "AIzaSyD-fake-fake-fake-fake-fake-fake-fake",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "sk-abcdefghijklmnopqrstuvwx",
    "123456789012",
    "someone@example.com",
    "arn:aws:iam::123456789012:role/geyser-claude",
    "10.0.1.23",
    "X-Amz-Signature=deadbeefcafe",
    "SuperSecretPassw0rd",
]


@pytest.mark.parametrize(
    "text",
    [
        "key AKIAABCDEFGHIJKLMNOP here",
        "ALPHAGENOME_API_KEY=AIzaSyD-fake-fake-fake-fake-fake-fake-fake",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-abcdefghijklmnopqrstuvwx",
        "account 123456789012 in eu-west-2",
        "mail someone@example.com",
        "arn:aws:iam::123456789012:role/geyser-claude",
        "host 10.0.1.23",
        "https://b.s3.amazonaws.com/k?X-Amz-Signature=deadbeefcafe&X-Amz-Date=1",
        "password: SuperSecretPassw0rd",
        "Authorization: Bearer abc.def.ghi1234567",
    ],
)
def test_redact_removes_secrets(text):
    out = rt.redact(text)
    assert not any(s in out for s in SECRETS) and "abc.def.ghi1234567" not in out


def test_personal_paths_and_names_are_redacted():
    extra = rt.personal_redactions(r"C:\Users\alice", "alice", ["Alice Example"])
    for text in (r"C:\Users\alice\.claude", "C:/Users/alice/x", "/c/Users/alice/Documents", "by Alice Example"):
        out = rt.redact(text, extra)
        assert "alice" not in out.lower(), out


def transcript_lines():
    session = "11111111-2222-3333-4444-555555555555"
    return [
        {
            "type": "user",
            "sessionId": session,
            "timestamp": "2026-11-03T10:00:00Z",
            "message": {"role": "user", "content": "My private prompt with someone@example.com and the plan"},
        },
        {
            "type": "assistant",
            "sessionId": session,
            "timestamp": "2026-11-03T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "A reply that must never be copied"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "cat ~/secret.tfvars # AKIAABCDEFGHIJKLMNOP"},
                    },
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "C:/x.py"}},
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session,
            "timestamp": "2026-11-03T10:00:06Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "PreToolUse:Bash hook error: reading *.tfvars is not allowed (account 123456789012)",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": "file contents with SuperSecretPassw0rd inside",
                    },
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session,
            "timestamp": "2026-11-03T10:01:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": '<cross-session-message from="uds:x" from-name="genomeflow-cloud-platform-50" from-mode="prompting">\n'
                        "Merge #192 now; the key is AIzaSyD-fake-fake-fake-fake-fake-fake-fake\nsecond line stays out\n</cross-session-message>",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": session,
            "timestamp": "2026-11-03T10:02:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "python edit_infra.py"}}
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session,
            "timestamp": "2026-11-03T10:02:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t3",
                        "is_error": True,
                        "content": "Permission for this action was denied by the Claude Code auto mode classifier. Reason: [Auto-Mode Bypass].",
                    }
                ],
            },
        },
    ]


@pytest.fixture
def transcripts(tmp_path):
    src = tmp_path / "projects" / "proj"
    src.mkdir(parents=True)
    path = src / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in transcript_lines()) + "not json\n", encoding="utf-8")
    return src, path.read_bytes()


def test_the_event_stream_keeps_only_the_allow_list(tmp_path, transcripts):
    src, before = transcripts
    out = tmp_path / "redacted"
    assert rt.main(["--input", str(src), "--out", str(out)]) == 0
    assert (src / "session.jsonl").read_bytes() == before  # the input is never modified
    events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert kinds.count("tool_call") == 3 and kinds.count("user_prompt") == 1 and kinds.count("peer_message") == 1
    hook = next(e for e in events if e.get("denial") == "hook")
    assert hook["tool"] == "Bash" and "<account-id>" in hook["text"] and "tfvars" in hook["text"]
    assert next(e for e in events if e.get("denial") == "classifier")["tool"] == "Bash"
    denied = [e for e in events if e["kind"] == "denied_command"]
    assert [d["command"] for d in denied][0].startswith("cat ~/secret.tfvars") and len(denied) == 2
    read = next(e for e in events if e["kind"] == "tool_result" and e["tool"] == "Read")
    assert read == {**read, "error": False} and "text" not in read  # a successful tool's output is never kept
    peer = next(e for e in events if e["kind"] == "peer_message")
    assert peer["from"] == "genomeflow-cloud-platform-50" and peer["first_line"].startswith("Merge #192 now")
    assert len(events[0]["session"]) == 12  # a hash, not the session ID
    everything = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    for text in [
        *SECRETS,
        "private prompt",
        "must never be copied",
        "file contents",
        "second line stays out",
        "11111111-2222-3333-4444-555555555555",
    ]:
        assert text not in everything, text
    summary = json.loads((out / "summary.json").read_text())
    assert summary["denial:hook"] == 1 and summary["denial:classifier"] == 1 and summary["unparsed_lines"] == 1
    assert "Check these lines" in (out / "spot_check.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("where", ["inside_input", "git_repo", "not_empty"])
def test_unsafe_output_folders_are_refused(tmp_path, transcripts, where):
    src, _ = transcripts
    if where == "inside_input":
        out = src / "redacted"
    elif where == "git_repo":
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        out = tmp_path / "repo" / "redacted"
    else:
        out = tmp_path / "used"
        out.mkdir()
        (out / "old.txt").write_text("x")
    with pytest.raises(SystemExit, match="refusing"):
        rt.main(["--input", str(src), "--out", str(out)])


@pytest.mark.parametrize(
    "path,new",
    [
        ("tests/{old.py => test_consistency.py}", "tests/test_consistency.py"),
        ("{app => scripts}/tool.py", "scripts/tool.py"),
        ("app/{ => sub}/x.py", "app/sub/x.py"),
        ("old.md => docs/new.md", "docs/new.md"),
        ("app/tasks.py", "app/tasks.py"),
    ],
)
def test_renamed_paths(path, new):
    assert mine_commits.renamed_path(path) == new
