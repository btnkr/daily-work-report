#!/usr/bin/env python3
"""Collect local work evidence and safely save an agent-written daily report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 2
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
NOISE_PREFIXES = (
    "# AGENTS.md instructions", "<environment_context>", "<skills_instructions>",
    "<turn_aborted>", "<local-command-caveat>", "<command-name>",
    "<user_shell_command>", "You are a coding agent", "Another language model",
    "<skill>", "<skill ", "<codex_internal_context", "<send_user_message_question_reply>",
)
CONTEXT_BLOCK = re.compile(
    r"<(skill|codex_internal_context|environment_context|skills_instructions|turn_aborted)\b[^>]*>"
    r".*?</\1\s*>", re.DOTALL,
)
QUESTION_REPLY = re.compile(
    r"<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>", re.DOTALL,
)
SECRET_PATTERNS = (
    r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_)[A-Za-z0-9_-]{16,}\b",
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*",
    r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+",
)
DEFAULTS = {
    "timezone": "Asia/Shanghai",
    "output_dir": "./daily-work-reports",
    "codex_home": os.environ.get("CODEX_HOME", "~/.codex"),
    "claude_home": "~/.claude",
    "project_roots": [],
    "include_dirs": [],
    "exclude_dirs": [],
    "exclude_session_ids": [],
    "project_names": {},
    "author_emails": [],
    "redact_patterns": [],
    "max_text_chars": 3000,
    "scan_depth": 4,
}


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def load_config(config_path: str | None = None, output_dir: str | None = None) -> dict:
    path = expand_path(config_path) if config_path else expand_path(
        os.environ.get("DAILY_WORK_REPORT_CONFIG", "~/.config/daily-work-report/config.json")
    )
    config = dict(DEFAULTS)
    if config_path or path.exists():
        supplied = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("Configuration must be a JSON object")
        unknown = supplied.keys() - DEFAULTS.keys()
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        config.update(supplied)
    if output_dir is not None:
        config["output_dir"] = output_dir
    for key in ("project_roots", "include_dirs", "exclude_dirs", "exclude_session_ids",
                "author_emails", "redact_patterns"):
        if not isinstance(config[key], list) or not all(isinstance(item, str) for item in config[key]):
            raise ValueError(f"{key} must be a list of strings")
    if not isinstance(config["project_names"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in config["project_names"].items()
    ):
        raise ValueError("project_names must map paths to names")
    for key in ("output_dir", "codex_home", "claude_home"):
        config[key] = expand_path(config[key])
    for key in ("project_roots", "include_dirs", "exclude_dirs"):
        config[key] = [expand_path(value) for value in config[key]]
    config["project_names"] = {str(expand_path(key)): value for key, value in config["project_names"].items()}
    config["tz"] = ZoneInfo(config["timezone"])
    for key in ("max_text_chars", "scan_depth"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    config["redactions"] = [re.compile(pattern) for pattern in (*SECRET_PATTERNS, *config["redact_patterns"])]
    return config


def parse_timestamp(value) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def day_bounds(date: str | None, tz) -> tuple[datetime, datetime]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz) if date else datetime.now(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=1)


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def allowed_path(value: str | None, config: dict) -> bool:
    if not value:
        return not config["include_dirs"]
    path = expand_path(value)
    if any(within(path, root) for root in config["exclude_dirs"]):
        return False
    return not config["include_dirs"] or any(within(path, root) for root in config["include_dirs"])


def text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance(block.get("text"), str)
        )
    return ""


def normalize_text(text: str) -> str:
    def reply_answers(match: re.Match) -> str:
        try:
            replies = json.loads(match.group(1))
        except json.JSONDecodeError:
            return ""
        if not isinstance(replies, list):
            return ""
        return "\n".join(
            f"用户补充：{reply['answer']}" for reply in replies
            if isinstance(reply, dict) and isinstance(reply.get("answer"), str) and reply["answer"].strip()
        )

    return CONTEXT_BLOCK.sub("", QUESTION_REPLY.sub(reply_answers, text)).strip()


def clean_text(text: str, config: dict) -> tuple[str, bool]:
    text = text.strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return "", False
    for pattern in config["redactions"]:
        text = pattern.sub("[REDACTED]", text)
    truncated = len(text) > config["max_text_chars"]
    return text[:config["max_text_chars"]] + ("…" if truncated else ""), truncated


def read_jsonl(path: Path, warnings: list[str]):
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f"Invalid JSON at {path}:{line_number}")
                    continue
                if isinstance(item, dict):
                    yield line_number, item
    except OSError as error:
        warnings.append(f"Cannot read {path}: {error.strerror}")


def event(path: Path, line: int, timestamp, role: str, text: str, config: dict, priority: int = 0) -> dict | None:
    at = parse_timestamp(timestamp)
    text = normalize_text(text)
    cleaned, truncated = clean_text(text, config)
    if at is None or not cleaned:
        return None
    return {
        "at": at.astimezone(config["tz"]).isoformat(),
        "role": role,
        "text": cleaned,
        "truncated": truncated,
        "source_file": str(path),
        "source_line": line,
        "_priority": priority,
        "_text_digest": hashlib.sha256(text.strip().encode()).hexdigest(),
    }


def deduplicate(events: list[dict], start: datetime, end: datetime) -> list[dict]:
    selected = []
    seen = defaultdict(list)
    for candidate in sorted(events, key=lambda item: -item["_priority"]):
        timestamp = parse_timestamp(candidate["at"])
        if not start <= timestamp < end:
            continue
        key = (candidate["role"], candidate["_text_digest"])
        duplicate = any(abs((previous - timestamp).total_seconds()) <= 2 for previous in seen[key])
        if not duplicate:
            selected.append(candidate)
            seen[key].append(timestamp)
    return sorted(selected, key=lambda item: (parse_timestamp(item["at"]), item["source_file"], item["source_line"]))


def collect_codex(config: dict, start: datetime, end: datetime, warnings: list[str]) -> list[dict]:
    sessions = []
    for directory in ("sessions", "archived_sessions"):
        root = config["codex_home"] / directory
        for path in sorted(root.rglob("*.jsonl")):
            metadata = {}
            events = []
            forked = False
            directories = set()
            for line, item in read_jsonl(path, warnings):
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = item.get("type")
                if kind in {"session_meta", "turn_context"} and payload.get("cwd"):
                    directories.add(payload["cwd"])
                if kind == "session_meta":
                    metadata = payload
                    source = payload.get("source")
                    forked = bool(payload.get("forked_from_id")) or (
                        isinstance(source, dict) and bool(source.get("subagent"))
                    )
                    continue
                message = None
                if kind == "event_msg":
                    subtype = payload.get("type")
                    if subtype in {"user_message", "agent_message"}:
                        role = "user" if subtype == "user_message" else "assistant"
                        if role == "assistant" and payload.get("phase") not in {None, "final_answer"}:
                            continue
                        message = event(path, line, item.get("timestamp"), role,
                                        text_content(payload.get("message")), config, 2)
                    elif subtype == "task_complete":
                        message = event(path, line, item.get("timestamp"), "assistant",
                                        text_content(payload.get("last_agent_message")), config, 1)
                elif kind == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    if role in {"user", "assistant"}:
                        if forked and role == "user":
                            started = parse_timestamp(metadata.get("timestamp"))
                            timestamp = parse_timestamp(item.get("timestamp"))
                            if started and timestamp and abs((timestamp - started).total_seconds()) <= 2:
                                continue
                        if role == "assistant" and payload.get("phase") not in {None, "final_answer"}:
                            continue
                        message = event(path, line, item.get("timestamp"), role,
                                        text_content(payload.get("content")), config)
                if message:
                    events.append(message)
            selected = deduplicate(events, start, end)
            session_id = metadata.get("id") or metadata.get("session_id") or path.stem
            cwd = metadata.get("cwd")
            if any(not allowed_path(directory, config) for directory in directories):
                continue
            if selected and session_id not in config["exclude_session_ids"] and allowed_path(cwd, config):
                sessions.append({"source": "codex", "id": session_id, "cwd": cwd,
                                 "parent_id": metadata.get("forked_from_id"), "events": selected})
    return sessions


def collect_claude(config: dict, start: datetime, end: datetime, warnings: list[str]) -> list[dict]:
    grouped = {}
    blocked = set()
    root = config["claude_home"]
    for path in sorted((root / "projects").rglob("*.jsonl")):
        if "subagents" in path.parts:
            continue
        events = []
        directories = set()
        session_id = path.stem
        for line, item in read_jsonl(path, warnings):
            if item.get("cwd"):
                directories.add(item["cwd"])
            session_id = item.get("sessionId") or session_id
            role = item.get("type")
            message = item.get("message")
            if role not in {"user", "assistant"} or not isinstance(message, dict):
                continue
            if item.get("isSidechain"):
                continue
            candidate = event(path, line, item.get("timestamp"), role,
                              text_content(message.get("content")), config, 1)
            if candidate:
                events.append(candidate)
        if directories and any(not allowed_path(cwd, config) for cwd in directories):
            blocked.add(session_id)
            grouped.pop(session_id, None)
            continue
        if not directories and config["include_dirs"]:
            continue
        selected = deduplicate(events, start, end)
        if selected and session_id not in config["exclude_session_ids"] and session_id not in blocked:
            entry = grouped.setdefault(session_id, {"source": "claude", "id": session_id,
                                                    "cwd": sorted(directories)[0] if directories else None,
                                                    "events": []})
            entry["events"].extend(selected)
    parsed_ids = set(grouped)
    for line, item in read_jsonl(root / "history.jsonl", warnings) if (root / "history.jsonl").exists() else []:
        session_id = item.get("sessionId")
        cwd = item.get("project")
        if not session_id or session_id in parsed_ids or session_id in blocked or session_id in config["exclude_session_ids"] or not allowed_path(cwd, config):
            continue
        candidate = event(root / "history.jsonl", line, item.get("timestamp"), "user",
                          text_content(item.get("display")), config)
        if candidate and start <= parse_timestamp(candidate["at"]) < end:
            entry = grouped.setdefault(session_id, {"source": "claude", "id": session_id,
                                                    "cwd": cwd, "events": []})
            entry["events"].append(candidate)
    return list(grouped.values())


def git(repo: Path, *args: str, warnings: list[str] | None = None) -> str:
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if result.returncode == 0:
            return result.stdout.rstrip("\n")
        if warnings is not None:
            warnings.append(f"Git {args[0]} failed for {repo}: {result.stderr.strip()[:300]}")
    except (OSError, subprocess.TimeoutExpired) as error:
        if warnings is not None:
            warnings.append(f"Git {args[0]} failed for {repo}: {error}")
        return ""
    return ""


def git_root(path: str | Path | None) -> Path | None:
    root = git(Path(path), "rev-parse", "--show-toplevel") if path else ""
    return Path(root).resolve() if root else None


def find_repos(config: dict, sessions: list[dict]) -> list[Path]:
    repos = set()
    for session in sessions:
        root = git_root(session["cwd"])
        if root and allowed_path(str(root), config):
            repos.add(root)
    for root in config["project_roots"]:
        for directory, subdirs, files in os.walk(root):
            path = Path(directory)
            if not allowed_path(str(path), config) and any(within(path, excluded) for excluded in config["exclude_dirs"]):
                subdirs[:] = []
                continue
            if ".git" in subdirs or ".git" in files:
                if allowed_path(str(path), config):
                    repos.add(path.resolve())
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS]
            if len(path.relative_to(root).parts) >= config["scan_depth"]:
                subdirs[:] = []
    return sorted(repos)


def collect_git(repo: Path, config: dict, start: datetime, end: datetime, warnings: list[str]) -> dict:
    emails = config["author_emails"] or [git(repo, "config", "user.email").strip()]
    emails = {email.casefold() for email in emails if email}
    commits = []
    if not emails:
        warnings.append(f"No author email for {repo}; commits omitted")
    else:
        output = git(repo, "log", "--all", "--no-merges", "--format=%H%x00%cI%x00%an%x00%ae%x00%s",
                     warnings=warnings)
        for record in output.splitlines():
            parts = record.split("\0", 4)
            if len(parts) != 5:
                continue
            commit_hash, timestamp, author, email, subject = parts
            at = parse_timestamp(timestamp)
            if email.casefold() in emails and at and start <= at < end:
                cleaned, truncated = clean_text(subject, config)
                commits.append({"hash": commit_hash, "at": at.astimezone(config["tz"]).isoformat(),
                                "author": author, "email": email, "subject": cleaned,
                                "truncated": truncated})
    status = git(repo, "status", "--porcelain=v1", "-z", warnings=warnings)
    return {
        "repo": str(repo), "author_emails": sorted(emails), "commits": commits,
        "working_tree": {
            "observed_at": datetime.now(config["tz"]).isoformat(timespec="seconds"),
            "historical": False,
            "status_porcelain": status.rstrip("\0").split("\0") if status else [],
            "diff_stat": git(repo, "diff", "--stat", "HEAD") if status else "",
        },
    }


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def build_payload(config: dict, date: str | None, exclude_ids: list[str] | None = None) -> dict:
    config = dict(config)
    config["exclude_session_ids"] = [*config["exclude_session_ids"], *(exclude_ids or [])]
    start, end = day_bounds(date, config["tz"])
    warnings = []
    if not any((config["codex_home"] / directory).is_dir() for directory in ("sessions", "archived_sessions")):
        warnings.append("Codex session directories not found")
    if not (config["claude_home"] / "projects").is_dir() and not (config["claude_home"] / "history.jsonl").is_file():
        warnings.append("Claude Code session sources not found")
    sessions = collect_codex(config, start, end, warnings) + collect_claude(config, start, end, warnings)
    unique_sessions = {}
    for session in sessions:
        key = (session["source"], session["id"])
        if key in unique_sessions:
            unique_sessions[key]["events"].extend(session["events"])
        else:
            unique_sessions[key] = session
    sessions = sorted(unique_sessions.values(), key=lambda item: (item["source"], item["id"]))
    repos = {str(repo): collect_git(repo, config, start, end, warnings) for repo in find_repos(config, sessions)}
    streams = defaultdict(list)
    for session in sessions:
        session["events"] = deduplicate(session["events"], start, end)
        for message in session["events"]:
            message["id"] = fingerprint([session["source"], session["id"], message["role"],
                                          message["at"], message["_text_digest"]])[:20]
            message.pop("_priority", None)
            message.pop("_text_digest", None)
        root = git_root(session["cwd"])
        key = str(root) if root else session["cwd"]
        if not root and (not key or expand_path(key) == Path.home() or within(expand_path(key), Path("/tmp"))):
            key = "__unattached__"
        streams[key].append(session)
    payload = {
        "schema_version": SCHEMA_VERSION, "date": start.date().isoformat(),
        "timezone": config["timezone"], "generated_at": datetime.now(config["tz"]).isoformat(timespec="seconds"),
        "counts": {"codex_sessions": sum(item["source"] == "codex" for item in sessions),
                   "claude_sessions": sum(item["source"] == "claude" for item in sessions)},
        "workstreams": [
            {"key": key, "name": config["project_names"].get(key, Path(key).name),
             "sessions": items, "git": repos.get(key)}
            for key, items in sorted(streams.items()) if key != "__unattached__"
        ],
        "git_only": [info for key, info in sorted(repos.items()) if key not in streams and info["commits"]],
        "unattached_sessions": streams.get("__unattached__", []),
        "warnings": sorted(set(warnings)),
    }
    stable = json.loads(json.dumps(payload))
    stable.pop("generated_at")
    snapshots = []
    for stream in stable["workstreams"]:
        if stream["git"]:
            snapshot = stream["git"].pop("working_tree")
            snapshot.pop("observed_at")
            snapshots.append({"repo": stream["git"]["repo"], "working_tree": snapshot})
    for info in stable["git_only"]:
        snapshot = info.pop("working_tree")
        snapshot.pop("observed_at")
        snapshots.append({"repo": info["repo"], "working_tree": snapshot})
    payload["evidence_digest"] = fingerprint(stable)
    payload["snapshot_digest"] = fingerprint(sorted(snapshots, key=lambda item: item["repo"]))
    return payload


def report_paths(output_dir: Path, date: str, mode: str) -> tuple[Path, Path]:
    suffix = ".brief" if mode == "brief" else ""
    return output_dir / "daily" / f"{date}{suffix}.md", output_dir / "_state" / f"{date}{suffix}.json"


def report_status(output_dir: Path, date: str, mode: str, digest: str) -> str:
    report, state = report_paths(output_dir, date, mode)
    if not report.exists():
        return "missing"
    try:
        saved = json.loads(state.read_text(encoding="utf-8"))
        content_hash = hashlib.sha256(report.read_bytes()).hexdigest()
        if saved["report_digest"] != content_hash:
            return "edited"
        return "up_to_date" if saved["evidence_digest"] == digest else "new_evidence"
    except (OSError, ValueError, KeyError, TypeError):
        return "unmanaged"


def save_report(output_dir: Path, evidence: dict, candidate: str, mode: str = "brief", replace: bool = False) -> dict:
    date = evidence["date"]
    datetime.strptime(date, "%Y-%m-%d")
    if not candidate.strip():
        raise ValueError("Report cannot be empty")
    target, state = report_paths(output_dir, date, mode)
    content = candidate.rstrip() + "\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    status = "saved"
    if target.exists() and not replace:
        if target.read_text(encoding="utf-8") == content:
            status = "unchanged"
        elif report_status(output_dir, date, mode, evidence["evidence_digest"]) == "up_to_date":
            return {"status": "up_to_date", "path": str(target)}
        else:
            draft = target.with_name(f"{target.stem}.draft-{digest[:12]}.md")
            if draft.exists() and draft.read_text(encoding="utf-8") != content:
                raise ValueError(f"Draft already exists with different content: {draft}")
            if not draft.exists():
                with draft.open("x", encoding="utf-8") as stream:
                    stream.write(content)
            return {"status": "draft", "path": str(draft), "preserved": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    if status == "saved":
        with target.open("w" if replace else "x", encoding="utf-8") as stream:
            stream.write(content)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"evidence_digest": evidence["evidence_digest"], "report_digest": digest},
                                indent=2) + "\n", encoding="utf-8")
    return {"status": status, "path": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Collect evidence without calling a model")
    collect.add_argument("--date", help="Local calendar date YYYY-MM-DD; default: today")
    collect.add_argument("--exclude-session", action="append", default=[], help="Exclude a session ID; repeatable")
    collect.add_argument("--print", dest="print_payload", action="store_true", help="Print evidence without writing it")
    save = commands.add_parser("save", help="Save Markdown; preserve existing reports by default")
    save.add_argument("--evidence", required=True, help="JSON file returned by collect")
    save.add_argument("--candidate", required=True, help="Agent-written Markdown file")
    save.add_argument("--replace", action="store_true", help="Explicitly replace an existing report")
    for command in (collect, save):
        command.add_argument("--config", help="Configuration JSON path")
        command.add_argument("--output-dir", help="Override output directory; save otherwise uses the evidence directory")
        command.add_argument("--mode", choices=("brief", "detailed"), default="brief")
    args = parser.parse_args()
    try:
        if args.command == "save":
            evidence_path = expand_path(args.evidence)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            candidate = expand_path(args.candidate).read_text(encoding="utf-8")
            output_dir = load_config(args.config, args.output_dir)["output_dir"] if (
                args.config or args.output_dir
            ) else evidence_path.parent.parent
            print(json.dumps(save_report(output_dir, evidence, candidate, args.mode, args.replace), ensure_ascii=False))
            return 0
        config = load_config(args.config, args.output_dir)
        payload = build_payload(config, args.date, args.exclude_session)
        if args.print_payload:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        out = config["output_dir"] / "_raw" / f"{payload['date']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"evidence_file": str(out), "evidence_digest": payload["evidence_digest"],
                          "snapshot_digest": payload["snapshot_digest"],
                          "report_file": str(report_paths(config["output_dir"], payload["date"], args.mode)[0]),
                          "report_status": report_status(config["output_dir"], payload["date"], args.mode,
                                                         payload["evidence_digest"]),
                          "counts": payload["counts"], "warnings": payload["warnings"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ZoneInfoNotFoundError, re.error) as error:
        print(f"daily-work-report: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
