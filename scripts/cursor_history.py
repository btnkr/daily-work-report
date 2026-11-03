"""只读采集 Cursor IDE 工作区及全局 SQLite 中带日期的消息。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


def is_wsl() -> bool:
    return sys.platform.startswith("linux") and (
        bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in os.uname().release.lower()
    )


def cursor_data_dirs(config: dict) -> list[Path]:
    # 按运行平台发现目录；显式配置时不再猜测其他位置。
    if config["sources"].get("cursor"):
        return sorted(set(config["sources"]["cursor"]))
    home = Path.home()
    if os.name == "nt":
        candidates = [Path(os.environ.get("APPDATA", str(home / "AppData/Roaming"))) / "Cursor/User"]
    elif sys.platform == "darwin":
        candidates = [home / "Library/Application Support/Cursor/User"]
    else:
        candidates = [Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "Cursor/User"]
    if is_wsl():
        for user in {home.name, os.environ.get("USERNAME", "")} - {""}:
            candidates.append(Path("/mnt/c/Users") / user / "AppData/Roaming/Cursor/User")
    return sorted({path.resolve() for path in candidates if path.is_dir()})


def local_path(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("fsPath") or value.get("path")
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("file:"):
        parsed = urlparse(value)
        if parsed.netloc not in {"", "localhost"}:
            return None
        value = unquote(parsed.path)
    elif "://" in value:
        return None
    if re.match(r"^/[A-Za-z]:[/\\]", value):
        # file:///C:/... 只移除盘符前的斜杠，盘符后的分隔符仍保留。
        value = value[1:]
    if re.match(r"^[A-Za-z]:[/\\]", value) and os.name != "nt":
        if not is_wsl():
            return None
        value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
    return str(Path(value).expanduser().resolve())


def json_value(value, location: str, warnings: list[str]):
    try:
        return json.loads(value)
    except (ValueError, TypeError, UnicodeDecodeError):
        warnings.append(f"Invalid Cursor JSON at {location}")
        return None


def database_rows(path: Path, table: str, prefix: str, warnings: list[str]) -> list[tuple]:
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
            # 只读连接仍可读取 WAL，兼容 Cursor 尚未关闭时的数据库。
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            return connection.execute(
                f'SELECT key, value FROM "{table}" WHERE key >= ? AND key < ? ORDER BY key',
                (prefix, prefix + "\uffff"),
            ).fetchall()
    except sqlite3.Error as error:
        error_code = getattr(error, "sqlite_errorcode", None)
        locked = error_code is not None and (error_code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        if locked or any(word in str(error).lower() for word in ("locked", "busy")):
            warnings.append(f"Cursor 数据库正被占用，可能正在写入，请稍后再试：{path}")
        else:
            warnings.append(f"Cannot read Cursor database {path}: {error}")
        return []


def collect_cursor(config: dict, start, end, warnings: list[str], make_event, allowed_path) -> list[dict]:
    sessions = []
    missing_dates = 0
    for data_dir in cursor_data_dirs(config):
        if not data_dir.is_dir():
            continue
        workspaces = {}
        composers = []
        references = {}
        for directory in sorted((data_dir / "workspaceStorage").glob("*")):
            metadata = directory / "workspace.json"
            cwd = None
            if metadata.is_file():
                try:
                    data = json_value(metadata.read_text(encoding="utf-8"), str(metadata), warnings)
                    if isinstance(data, dict):
                        cwd = local_path(data.get("folder"))
                except OSError as error:
                    warnings.append(f"Cannot read Cursor workspace {metadata}: {error}")
            workspaces[directory.name] = cwd
            database = directory / "state.vscdb"
            for key, value in database_rows(database, "ItemTable", "composer.composerData", warnings):
                if key != "composer.composerData":
                    continue
                data = json_value(value, f"{database}:{key}", warnings)
                if not isinstance(data, dict):
                    continue
                entries = data.get("allComposers", [])
                if not isinstance(entries, list):
                    continue
                for composer in entries:
                    if not isinstance(composer, dict):
                        continue
                    session_id = composer.get("composerId")
                    if isinstance(session_id, str) and session_id:
                        references.setdefault(session_id, set()).add(cwd)
                        composers.append((database, key, composer, cwd))
                    else:
                        warnings.append(f"Skipped Cursor composer with invalid ID at {database}:{key}")
        global_db = data_dir / "globalStorage/state.vscdb"
        global_headers = {}
        for key, value in database_rows(global_db, "cursorDiskKV", "composerData:", warnings):
            composer = json_value(value, f"{global_db}:{key}", warnings)
            if not isinstance(composer, dict):
                continue
            session_id = composer.get("composerId") or key.removeprefix("composerData:")
            if not isinstance(session_id, str):
                warnings.append(f"Skipped Cursor composer with invalid ID at {global_db}:{key}")
                continue
            composer["composerId"] = session_id
            if isinstance(composer.get("fullConversationHeadersOnly"), list):
                global_headers[session_id] = composer["fullConversationHeadersOnly"]
            identity = composer.get("workspaceIdentifier") or {}
            identity = identity if isinstance(identity, dict) else {}
            workspace_id = identity.get("id")
            cwd = local_path(identity.get("uri")) or workspaces.get(workspace_id if isinstance(workspace_id, str) else None)
            linked = references.get(session_id, set())
            if cwd is None and len(linked) == 1:
                cwd = next(iter(linked))
            composers.append((global_db, key, composer, cwd))
        found_records = False
        seen_messages = set()
        # 全局正文优先；工作区旧副本只补充尚未读到的有效消息。
        for database, key, composer, cwd in sorted(composers, key=lambda item: item[0] != global_db):
            session_id = composer.get("composerId")
            if not session_id or session_id in config["exclude_session_ids"]:
                continue
            linked = references.get(session_id, set())
            if any(not allowed_path(path, config) for path in linked) or not allowed_path(cwd, config):
                continue
            bubbles = []
            inline = composer.get("conversation", [])
            # 全局有效列表约束同一会话的所有副本，包括旧工作区内联正文。
            headers = global_headers.get(session_id, composer.get("fullConversationHeadersOnly"))
            active_ids = None
            # 两种存储形式遵循同一有效列表，防止旧分支结论混入日报。
            if isinstance(headers, list):
                active_ids = {header["bubbleId"] for header in headers
                              if isinstance(header, dict) and isinstance(header.get("bubbleId"), str)}
            if isinstance(inline, list):
                for index, bubble in enumerate(inline):
                    bubble_id = bubble.get("bubbleId") if isinstance(bubble, dict) else None
                    if active_ids is not None and (not isinstance(bubble_id, str) or bubble_id not in active_ids):
                        continue
                    bubbles.append((f"{key}:conversation:{index}", bubble))
            if database == global_db:
                standalone = []
                for bubble_key, value in database_rows(global_db, "cursorDiskKV", f"bubbleId:{session_id}:", warnings):
                    if active_ids is not None and bubble_key.removeprefix(f"bubbleId:{session_id}:") not in active_ids:
                        continue
                    bubble = json_value(value, f"{global_db}:{bubble_key}", warnings)
                    standalone.append((bubble_key, bubble))
                # 独立消息是全局正文的首选，旧内联内容只作为缺失消息的回退。
                bubbles = standalone + bubbles
            selected = []
            for source_key, bubble in bubbles:
                if not isinstance(bubble, dict):
                    continue
                bubble_id = source_key.removeprefix(f"bubbleId:{session_id}:") if source_key.startswith(
                    f"bubbleId:{session_id}:"
                ) else bubble.get("bubbleId")
                message_key = (session_id, bubble_id) if isinstance(bubble_id, str) else None
                if message_key is not None and message_key in seen_messages:
                    continue
                bubble_type = bubble.get("type")
                if type(bubble_type) not in (int, str):
                    warnings.append(f"Skipped Cursor message with invalid type at {database}:{source_key}")
                    continue
                role = {1: "user", 2: "assistant", "user": "user", "assistant": "assistant"}.get(bubble_type)
                text = bubble.get("text")
                if role is None or not isinstance(text, str) or not text.strip():
                    continue
                found_records = True
                # 必须使用逐条消息的日期，不能用会话更新时间替代。
                candidate = make_event(database, 0, bubble.get("createdAt") or bubble.get("timestamp"),
                                       role, text, config, 2)
                if candidate is None:
                    if bubble.get("createdAt") is None and bubble.get("timestamp") is None:
                        missing_dates += 1
                    elif candidate is None:
                        warnings.append(f"Skipped Cursor message without usable text or timestamp at {database}:{source_key}")
                    continue
                timestamp = candidate["at"]
                if not start <= datetime.fromisoformat(timestamp) < end:
                    continue
                if message_key is not None:
                    seen_messages.add(message_key)
                candidate.update({"source_key": source_key, "cwd": cwd})
                selected.append(candidate)
            if selected:
                if cwd is None:
                    warnings.append(f"Cursor workspace unavailable for session {session_id}; kept unattached")
                sessions.append({"source": "cursor", "id": session_id, "cwd": cwd, "events": selected})
        if not composers or not found_records:
            warnings.append(f"No supported Cursor composer messages found in {data_dir}")
    if missing_dates:
        warnings.append(f"Skipped {missing_dates} Cursor messages without per-message dates; no date inferred from session or file")
    return sessions
