"""统一消息、时间和目录范围，完成清洗、脱敏及去重。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config_settings import expand_path

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
    # 自动注入的指令不算工作内容；问答回执只保留用户实际回答。
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
    # 保留原文摘要及来源位置，避免截断后误合并，也便于必要时回查。
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
    # 同一消息可能以多种日志事件重复出现，优先保留高优先级的表示。
    selected = []
    seen = defaultdict(list)
    for candidate in sorted(events, key=lambda item: -item["_priority"]):
        timestamp = parse_timestamp(candidate["at"])
        if not start <= timestamp < end:
            continue
        key = (candidate["role"], candidate["_text_digest"], candidate.get("cwd"))
        duplicate = any(abs((previous - timestamp).total_seconds()) <= 2 for previous in seen[key])
        if not duplicate:
            selected.append(candidate)
            seen[key].append(timestamp)
    return sorted(selected, key=lambda item: (parse_timestamp(item["at"]), item["source_file"], item["source_line"]))



def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

