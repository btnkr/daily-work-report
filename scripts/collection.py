"""汇总目标日期的多工具会话与 Git 证据。"""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config_settings import expand_path
from cursor_history import collect_cursor
from evidence import allowed_path, day_bounds, deduplicate, event, fingerprint, within
from git_activity import collect_git, find_repos, git_root
from session_history import collect_claude, collect_codex, source_dirs

SCHEMA_VERSION = 2



def build_payload(config: dict, date: str | None, exclude_ids: list[str] | None = None) -> dict:
    config = dict(config)
    config["exclude_session_ids"] = [*config["exclude_session_ids"], *(exclude_ids or [])]
    start, end = day_bounds(date, config["tz"])
    warnings = []
    detected = {
        "codex": any((root / directory).is_dir() for root in source_dirs(config, "codex")
                     for directory in ("sessions", "archived_sessions")),
        "claude": any((root / "projects").is_dir() or (root / "history.jsonl").is_file()
                      for root in source_dirs(config, "claude")),
        "cursor": any(path.is_dir() for path in source_dirs(config, "cursor")),
    }
    for tool, paths in config["sources"].items():
        for path in paths:
            if not path.is_dir():
                warnings.append(f"Configured {tool} data directory not found: {path}")
    sessions = collect_codex(config, start, end, warnings) + collect_claude(config, start, end, warnings)
    sessions += collect_cursor(config, start, end, warnings, event, allowed_path)
    unique_sessions = {}
    # 同一会话可能跨项目，按消息的实际目录拆分后再关联仓库。
    for session in sessions:
        for message in session["events"]:
            cwd = message.get("cwd", session["cwd"])
            if not allowed_path(cwd, config):
                continue
            key = (session["source"], session["id"], cwd or "")
            entry = unique_sessions.setdefault(key, {**session, "cwd": cwd, "events": []})
            entry["events"].append(message)
    sessions = [value for _, value in sorted(unique_sessions.items())]
    repos = {str(repo): collect_git(repo, config, start, end, warnings) for repo in find_repos(config, sessions, warnings)}
    streams = defaultdict(list)
    roots = {}
    for session in sessions:
        session["events"] = deduplicate(session["events"], start, end)
        for message in session["events"]:
            message["id"] = fingerprint([session["source"], session["id"], message["role"],
                                          message["at"], message["_text_digest"]])[:20]
            message.pop("_priority", None)
            message.pop("_text_digest", None)
        cwd = session["cwd"]
        if cwd not in roots:
            roots[cwd] = git_root(cwd)
        root = roots[cwd]
        key = str(root) if root else session["cwd"]
        if not root and (not key or expand_path(key) == Path.home()
                         or within(expand_path(key), Path(tempfile.gettempdir()).resolve())):
            key = "__unattached__"
        streams[key].append(session)
    payload = {
        "schema_version": SCHEMA_VERSION, "date": start.date().isoformat(),
        "timezone": config["timezone"], "generated_at": datetime.now(config["tz"]).isoformat(timespec="seconds"),
        "sources": {source: {"detected": available} for source, available in detected.items()},
        "counts": {f"{source}_sessions": len({item["id"] for item in sessions if item["source"] == source})
                   for source in detected},
        "workstreams": [
            {"key": key, "name": config["project_names"].get(key, Path(key).name),
             "sessions": items, "git": repos.get(key)}
            for key, items in sorted(streams.items()) if key != "__unattached__"
        ],
        "git_only": [info for key, info in sorted(repos.items()) if key not in streams and info["commits"]],
        "unattached_sessions": streams.get("__unattached__", []),
        "warnings": sorted(set(warnings)),
    }
    payload["has_evidence"] = bool(sessions or payload["git_only"])
    if not payload["has_evidence"]:
        payload["warnings"].append("No dated sessions or matching author commits found; do not generate a report")
    stable = json.loads(json.dumps(payload))
    # 日期素材和当前快照分别计算摘要，今天的脏文件不触发历史日报更新。
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

