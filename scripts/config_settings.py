"""读取和校验个人配置，安全更新长期偏好。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULTS = {
    "timezone": "Asia/Shanghai",
    "output_dir": "~/daily-work-reports",
    "style": "brief",
    "sources": {},
    "project_roots": [],
    "repositories": [],
    "include_dirs": [],
    "exclude_dirs": [],
    "exclude_session_ids": [],
    "project_names": {},
    "author_emails": [],
    "redact_patterns": [],
    "max_text_chars": 3000,
    "scan_depth": 4,
}

SECRET_PATTERNS = (
    r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_)[A-Za-z0-9_-]{16,}\b",
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*",
    r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+",
)


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def config_file(config_path: str | None = None) -> Path:
    return expand_path(config_path or os.environ.get(
        "DAILY_WORK_REPORT_CONFIG", "~/.config/daily-work-report/config.json"
    ))


def normalize_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("Configuration must be a JSON object")
    settings = dict(settings)
    sources = settings.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("sources must map tool names to lists of data directories")
    sources = dict(sources)
    # 兼容早期字段，内部统一使用 sources.<工具名> 的目录列表。
    for old_key, tool in (("codex_home", "codex"), ("claude_home", "claude"), ("cursor_data_dirs", "cursor")):
        if old_key in settings:
            old_value = settings.pop(old_key)
            sources.setdefault(tool, old_value if tool == "cursor" else [old_value])
    if sources.keys() - {"codex", "claude", "cursor"}:
        raise ValueError("Unknown source: " + ", ".join(sorted(sources.keys() - {"codex", "claude", "cursor"})))
    for tool, paths in sources.items():
        if not isinstance(paths, list) or not all(isinstance(path, str) and path.strip() for path in paths):
            raise ValueError(f"sources.{tool} must be a list of non-empty directory paths")
    settings["sources"] = sources
    return settings


def load_config(config_path: str | None = None, output_dir: str | None = None) -> dict:
    path = config_file(config_path)
    config = dict(DEFAULTS)
    if config_path or os.environ.get("DAILY_WORK_REPORT_CONFIG") or path.exists():
        supplied = normalize_settings(json.loads(path.read_text(encoding="utf-8")))
        unknown = supplied.keys() - DEFAULTS.keys()
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        config.update(supplied)
    if output_dir is not None:
        config["output_dir"] = output_dir
    list_keys = ("project_roots", "repositories", "include_dirs", "exclude_dirs", "exclude_session_ids",
                 "author_emails", "redact_patterns")
    for key in list_keys:
        if not isinstance(config[key], list) or not all(isinstance(item, str) for item in config[key]):
            raise ValueError(f"{key} must be a list of strings")
    if not isinstance(config["project_names"], dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in config["project_names"].items()
    ):
        raise ValueError("project_names must map paths to names")
    if not isinstance(config["output_dir"], str) or not config["output_dir"].strip():
        raise ValueError("output_dir must be a non-empty path string")
    config["output_dir"] = expand_path(config["output_dir"])
    for key in ("project_roots", "repositories", "include_dirs", "exclude_dirs"):
        config[key] = [expand_path(value) for value in config[key]]
    config["sources"] = {tool: [expand_path(path) for path in paths]
                         for tool, paths in config["sources"].items()}
    config["project_names"] = {str(expand_path(key)): value for key, value in config["project_names"].items()}
    if not isinstance(config["timezone"], str):
        raise ValueError("timezone must be an IANA timezone name")
    try:
        config["tz"] = ZoneInfo(config["timezone"])
    except ZoneInfoNotFoundError as error:
        raise ValueError("Timezone unavailable: " + config["timezone"]
                         + ". Check the IANA name; if system timezone data is missing, run: python -m pip install tzdata") from error
    if config["style"] not in ("brief", "detailed"):
        raise ValueError("style must be brief or detailed")
    for key in ("max_text_chars", "scan_depth"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    config["redactions"] = [re.compile(pattern) for pattern in (*SECRET_PATTERNS, *config["redact_patterns"])]
    config["current_dir"] = Path.cwd()
    return config


def atomic_write(path: Path, content: str) -> None:
    """统一使用 UTF-8 和 LF 换行，再原子替换，避免平台差异。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def update_config(config_path: str | None, changes: dict) -> Path:
    """先合并并校验，再写回配置；无效变更不会覆盖原文件。"""
    path = config_file(config_path)
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Configuration changes must be a non-empty JSON object")
    changes = normalize_settings(changes)
    if changes.keys() - DEFAULTS.keys():
        raise ValueError("Unknown configuration keys: " + ", ".join(sorted(changes.keys() - DEFAULTS.keys())))
    current = normalize_settings(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
    updated = {**current, **changes}
    # 更新一个工具的数据目录时，保留其他工具的设置。
    updated["sources"] = {**current.get("sources", {}), **changes.get("sources", {})}
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "config.json"
        candidate.write_text(json.dumps(updated), encoding="utf-8")
        load_config(str(candidate))
    atomic_write(path, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    return path
