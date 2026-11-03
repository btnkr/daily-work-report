"""将 Codex、Claude Code 的本机历史转换为统一会话结构。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from config_settings import expand_path
from cursor_history import cursor_data_dirs
from evidence import allowed_path, deduplicate, event, parse_timestamp, read_jsonl, text_content

def source_dirs(config: dict, tool: str) -> list[Path]:
    # 自定义目录优先；未配置时沿用各工具默认位置。
    configured = config["sources"].get(tool, [])
    if configured:
        return sorted(set(configured))
    if tool == "cursor":
        return cursor_data_dirs(config)
    default = os.environ.get("CODEX_HOME", "~/.codex") if tool == "codex" else "~/.claude"
    return [expand_path(default)]



def collect_codex(config: dict, start: datetime, end: datetime, warnings: list[str]) -> list[dict]:
    return [session for root in source_dirs(config, "codex")
            for session in collect_codex_root(root, config, start, end, warnings)]



def collect_codex_root(home: Path, config: dict, start: datetime, end: datetime, warnings: list[str]) -> list[dict]:
    sessions = []
    for directory in ("sessions", "archived_sessions"):
        root = home / directory
        for path in sorted(root.rglob("*.jsonl")):
            metadata = {}
            events = []
            forked = False
            directories = set()
            cwd = None
            for line, item in read_jsonl(path, warnings):
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = item.get("type")
                if kind in {"session_meta", "turn_context"} and payload.get("cwd"):
                    directories.add(payload["cwd"])
                    cwd = payload["cwd"]
                if kind == "session_meta":
                    metadata = payload
                    source = payload.get("source")
                    forked = bool(payload.get("forked_from_id")) or (
                        isinstance(source, dict) and bool(source.get("subagent"))
                    )
                    continue
                timestamp = parse_timestamp(item.get("timestamp"))
                if timestamp is None or not start <= timestamp < end:
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
                    message["cwd"] = cwd
                    events.append(message)
            selected = deduplicate(events, start, end)
            session_id = metadata.get("id") or metadata.get("session_id") or path.stem
            if any(not allowed_path(directory, config) for directory in directories):
                # 混合目录会话保守整条排除，防止上下文夹带范围外的内容。
                continue
            if selected and session_id not in config["exclude_session_ids"] and allowed_path(cwd, config):
                sessions.append({"source": "codex", "id": session_id, "cwd": cwd,
                                 "parent_id": metadata.get("forked_from_id"), "events": selected})
    return sessions



def collect_claude(config: dict, start: datetime, end: datetime, warnings: list[str]) -> list[dict]:
    return [session for root in source_dirs(config, "claude")
            for session in collect_claude_root(root, config, start, end, warnings)]



def collect_claude_root(root: Path, config: dict, start: datetime, end: datetime, warnings: list[str]) -> list[dict]:
    grouped = {}
    blocked = set()
    for path in sorted((root / "projects").rglob("*.jsonl")):
        if "subagents" in path.parts:
            continue
        events = []
        directories = set()
        session_id = path.stem
        cwd = None
        for line, item in read_jsonl(path, warnings):
            if item.get("cwd"):
                directories.add(item["cwd"])
                cwd = item["cwd"]
            session_id = item.get("sessionId") or session_id
            role = item.get("type")
            message = item.get("message")
            if role not in {"user", "assistant"} or not isinstance(message, dict):
                continue
            if item.get("isSidechain"):
                continue
            timestamp = parse_timestamp(item.get("timestamp"))
            if timestamp is None or not start <= timestamp < end:
                continue
            candidate = event(path, line, item.get("timestamp"), role,
                              text_content(message.get("content")), config, 1)
            if candidate:
                candidate["cwd"] = cwd
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
    # 仅在缺少正文时读取 history 提问，避免重复采集同一会话。
    for line, item in read_jsonl(root / "history.jsonl", warnings) if (root / "history.jsonl").exists() else []:
        session_id = item.get("sessionId")
        cwd = item.get("project")
        if not session_id or session_id in parsed_ids or session_id in blocked or session_id in config["exclude_session_ids"] or not allowed_path(cwd, config):
            continue
        candidate = event(root / "history.jsonl", line, item.get("timestamp"), "user",
                          text_content(item.get("display")), config)
        if candidate and start <= parse_timestamp(candidate["at"]) < end:
            candidate["cwd"] = cwd
            entry = grouped.setdefault(session_id, {"source": "claude", "id": session_id,
                                                    "cwd": cwd, "events": []})
            entry["events"].append(candidate)
    return list(grouped.values())

