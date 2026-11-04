#!/usr/bin/env python3
"""RP-2: a redacted event stream from Claude Code session transcripts. **The user runs this, not an agent.**

    python scripts/rp2/redact_transcripts.py --input <~/.claude/projects/<project>> --out <new folder outside every repo>

Reads every *.jsonl under --input (read-only) and writes to --out:
- events.jsonl: one line per event, from an ALLOW-LIST of fields only;
- summary.json: counts per kind;
- spot_check.txt: SPOT_CHECK random output lines for the user to inspect before anyone reads the output.

What an event keeps:
- tool_call: session (a hash, not the ID), time, tool name, call id;
- tool_result: the same, plus error yes/no, a denial class (hook, classifier, permission rule, user rejected), and
  for errors and denials only, the first TEXT_LIMIT characters of the message, redacted;
- denied_command: for a denied or failed Bash call, the command's first line, redacted and truncated;
- peer_message: sender name, time, the first line of the message, redacted and truncated;
- user_prompt / assistant_text: the character count only. No prompt or reply text is ever copied.

Everything else (tool outputs, file contents, prompts, replies, thinking) is dropped. What is kept goes through
redact(): AWS keys and account IDs, ARNs, e-mail addresses, IPv4 addresses, API keys and tokens (AWS, Google,
GitHub, sk-, Bearer, key=/token=/secret=/password=), presigned-URL queries, JWTs, home paths and the user's name.

Safety: --out must not exist or must be empty, must be outside every git repository and outside --input. The input
is opened read-only and never modified. Standard library only.
"""

import argparse
import glob
import hashlib
import json
import os
import random
import re
import sys

TEXT_LIMIT = 300
COMMAND_LIMIT = 160
SPOT_CHECK = 20

# (pattern, replacement), applied in order. Longest and most specific first.
REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<private-key>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<aws-access-key>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "<google-api-key>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "<github-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "<github-token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<api-key>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <token>"),
    (re.compile(r"(?i)(aws_secret_access_key|aws_session_token)(\s*[=:]\s*)\S+"), r"\1\2<secret>"),
    (
        re.compile(r"(?i)\b([A-Za-z_]*(?:api[_-]?key|token|secret|password|passwd))(\s*[=:]\s*)[\"']?[^\s\"'&]+"),
        r"\1\2<secret>",
    ),
    (re.compile(r"[?&]X-Amz-[A-Za-z-]+=[^\s\"']*"), "?<presigned-query>"),
    (re.compile(r"\barn:aws[a-z-]*:[^\s\"'`,)]+"), "<arn>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"(?<![\d.])\d{12}(?![\d.])"), "<account-id>"),
]


def personal_redactions(home: str, user: str, names=()) -> list:
    """The home folder (every spelling) and the user's names, which only the person running this knows."""
    pairs = []
    variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
    if len(home) > 2 and home[1] == ":":  # C:\Users\x -> /c/Users/x (Git Bash)
        variants.add("/" + home[0].lower() + home.replace("\\", "/")[2:])
    for v in sorted(variants, key=len, reverse=True):
        if v:
            pairs.append((re.compile(re.escape(v), re.I), "~"))
    for name in [user, *names]:
        if name and len(name) >= 3:
            pairs.append((re.compile(r"\b" + re.escape(name) + r"\b", re.I), "<user>"))
    return pairs


def redact(text, extra=()) -> str:
    text = str(text)
    for pattern, replacement in [*extra, *REDACTIONS]:
        text = pattern.sub(replacement, text)
    return text


def denial_class(text: str):
    t = text.lower()
    if "hook" in t and ("pretooluse" in t or "blocked" in t or "hook error" in t):
        return "hook"
    if "auto mode classifier" in t or ("classifier" in t and "denied" in t):
        return "classifier"
    if "permission" in t and "denied" in t:
        return "permission_rule"
    if "user doesn't want to proceed" in t or ("rejected" in t and "user" in t):
        return "user_rejected"
    return None


def _session(record) -> str:
    sid = record.get("sessionId") or record.get("session_id") or ""
    return hashlib.sha256(sid.encode()).hexdigest()[:12] if sid else ""


def _blocks(message):
    content = (message or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _text_of(block) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return block.get("text") or ""


PEER = re.compile(r'<cross-session-message from="[^"]*"(?: from-name="([^"]*)")?[^>]*>\s*(.*?)\s*(?:</cross|$)', re.S)


def _strings(obj):
    """Every string inside a JSON value (peer messages may sit outside the message blocks)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value)


def events(record, calls: dict, extra) -> list:
    """The allow-listed events of one transcript line. calls: tool_use id -> (name, command), filled as we go."""
    out = []
    base = {"session": _session(record), "time": record.get("timestamp")}
    kind = record.get("type")
    message = record.get("message") or {}
    for block in _blocks(message):
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name") or "?"
            command = (block.get("input") or {}).get("command") if isinstance(block.get("input"), dict) else None
            calls[block.get("id")] = (name, command)
            out.append({**base, "kind": "tool_call", "tool": name, "call": block.get("id")})
        elif btype == "tool_result":
            name, command = calls.get(block.get("tool_use_id"), ("?", None))
            text = _text_of(block)
            denied = denial_class(text)
            error = bool(block.get("is_error")) or denied is not None
            event = {**base, "kind": "tool_result", "tool": name, "call": block.get("tool_use_id"), "error": error}
            if denied:
                event["denial"] = denied
            if error:
                event["text"] = redact(text[: TEXT_LIMIT * 2], extra)[:TEXT_LIMIT]
            out.append(event)
            if error and command:
                first = command.strip().splitlines()[0] if command.strip() else ""
                out.append(
                    {
                        **base,
                        "kind": "denied_command" if denied else "failed_command",
                        "tool": name,
                        "call": block.get("tool_use_id"),
                        "command": redact(first, extra)[:COMMAND_LIMIT],
                    }
                )
        elif btype == "text":
            text = block.get("text") or ""
            if "<cross-session-message" in text:
                continue  # counted below as a peer message, never as a prompt
            if kind == "user":
                out.append({**base, "kind": "user_prompt", "chars": len(text)})
            elif kind == "assistant":
                out.append({**base, "kind": "assistant_text", "chars": len(text)})
    for text in _strings(record):
        for peer in PEER.finditer(text) if "<cross-session-message" in text else ():
            first = (peer.group(2) or "").strip().splitlines()
            out.append(
                {
                    **base,
                    "kind": "peer_message",
                    "from": redact(peer.group(1) or "?", extra),
                    "first_line": redact(first[0] if first else "", extra)[:COMMAND_LIMIT],
                }
            )
    return out


def _inside_git_repo(path: str) -> bool:
    path = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def check_out(out: str, input_dir: str) -> None:
    out_abs, in_abs = os.path.abspath(out), os.path.abspath(input_dir)
    if out_abs == in_abs or out_abs.startswith(in_abs + os.sep):
        raise SystemExit("refusing: --out is inside --input")
    if _inside_git_repo(out_abs):
        raise SystemExit(f"refusing: {out} is inside a git repository (choose a new folder outside every repo)")
    if os.path.isdir(out_abs) and os.listdir(out_abs):
        raise SystemExit(f"refusing: {out} is not empty (choose a new folder)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="a transcripts folder, e.g. ~/.claude/projects/<project>")
    p.add_argument("--out", required=True, help="a NEW folder outside every git repository")
    p.add_argument("--name", action="append", default=[], help="another name to redact (repeatable)")
    p.add_argument("--seed", type=int, default=0, help="for the spot-check sample")
    a = p.parse_args(argv)
    input_dir = os.path.expanduser(a.input)
    if not os.path.isdir(input_dir):
        raise SystemExit(f"no such folder: {input_dir}")
    out = os.path.expanduser(a.out)
    check_out(out, input_dir)
    os.makedirs(out, exist_ok=True)
    home = os.path.expanduser("~")
    extra = personal_redactions(home, os.path.basename(home), a.name)

    counts, lines, files = {}, [], sorted(glob.glob(os.path.join(input_dir, "**", "*.jsonl"), recursive=True))
    with open(os.path.join(out, "events.jsonl"), "w", encoding="utf-8") as dst:
        for path in files:
            calls = {}
            with open(path, encoding="utf-8", errors="replace") as src:
                for raw in src:
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        counts["unparsed_lines"] = counts.get("unparsed_lines", 0) + 1
                        continue
                    if not isinstance(record, dict):
                        continue
                    for event in events(record, calls, extra):
                        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
                        if event.get("denial"):
                            key = f"denial:{event['denial']}"
                            counts[key] = counts.get(key, 0) + 1
                        line = json.dumps(event, ensure_ascii=False)
                        dst.write(line + "\n")
                        lines.append(line)
    counts["transcript_files"] = len(files)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(counts, fh, indent=2, sort_keys=True)
    sample = random.Random(a.seed).sample(lines, min(SPOT_CHECK, len(lines)))
    with open(os.path.join(out, "spot_check.txt"), "w", encoding="utf-8") as fh:
        fh.write("Check these lines for anything personal or secret before anyone reads events.jsonl.\n\n")
        fh.write("\n".join(sample) + "\n")
    print(f"{len(files)} transcripts -> {len(lines)} events in {out} (see summary.json; spot-check spot_check.txt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
