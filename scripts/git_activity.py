"""发现仓库，并按作者、日期和目录范围采集 Git 活动。"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from evidence import allowed_path, clean_text, parse_timestamp, within

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}



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


def intersects_scope(path: Path, config: dict) -> bool:
    """仓库可以是纳入子目录的祖先，具体文件范围仍由采集时过滤。"""
    if any(within(path, excluded) for excluded in config["exclude_dirs"]):
        return False
    return not config["include_dirs"] or any(
        within(path, included) or (within(included, path) and allowed_path(str(included), config))
        for included in config["include_dirs"]
    )



def find_repos(config: dict, sessions: list[dict], warnings: list[str] | None = None) -> list[Path]:
    # 当前工作区、会话、明确清单和父目录扫描的结果合并去重。
    repos = set()
    current = git_root(config["current_dir"])
    if current and intersects_scope(current, config):
        repos.add(current)
    for session in sessions:
        root = git_root(session["cwd"])
        if root and intersects_scope(root, config):
            repos.add(root)
    for path in config["repositories"]:
        if not intersects_scope(path, config):
            continue
        root = git_root(path)
        if root and intersects_scope(root, config):
            repos.add(root)
        elif root is None and warnings is not None:
            warnings.append(f"Configured repository unavailable or not a Git repository: {path}")
    for root in config["project_roots"]:
        for directory, subdirs, files in os.walk(root):
            path = Path(directory)
            if not allowed_path(str(path), config) and any(within(path, excluded) for excluded in config["exclude_dirs"]):
                subdirs[:] = []
                continue
            if ".git" in subdirs or ".git" in files:
                if intersects_scope(path, config):
                    repos.add(path.resolve())
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS]
            if len(path.relative_to(root).parts) >= config["scan_depth"]:
                subdirs[:] = []
    return sorted(repos)



def commit_records(repo: Path, warnings: list[str]):
    # 逐条检查日期，避免提交时间乱序时被 Git 的提前截止规则漏掉。
    command = ["git", "--no-optional-locks", "-C", str(repo), "log", "--all", "--no-merges",
               "--format=%H%x00%cI%x00%an%x00%ae%x00%s"]
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, timeout=30)
            if result.returncode:
                detail = result.stderr.decode("utf-8", errors="replace")
                if "does not have any commits yet" not in detail:
                    warnings.append(f"Git log failed for {repo}: {detail[:300]}")
                return
            stream.seek(0)
            yield from stream
    except (OSError, subprocess.TimeoutExpired) as error:
        warnings.append(f"Git log failed for {repo}: {error}")



def collect_git(repo: Path, config: dict, start: datetime, end: datetime, warnings: list[str]) -> dict:
    emails = config["author_emails"] or [git(repo, "config", "user.email").strip()]
    emails = {email.casefold() for email in emails if email}
    commits = []
    if not emails:
        warnings.append(f"No author email for {repo}; commits omitted")
    else:
        for record in commit_records(repo, warnings):
            parts = record.rstrip("\n").split("\0", 4)
            if len(parts) != 5:
                continue
            commit_hash, timestamp, author, email, subject = parts
            at = parse_timestamp(timestamp)
            if email.casefold() in emails and at and start <= at < end and not commit_is_excluded(repo, commit_hash, config, warnings):
                cleaned, truncated = clean_text(subject, config)
                commits.append({"hash": commit_hash, "at": at.astimezone(config["tz"]).isoformat(),
                                "author": author, "email": email, "subject": cleaned,
                                "truncated": truncated})
    status = filtered_status(repo, config, warnings)
    scoped = bool(config["exclude_dirs"] or config["include_dirs"])
    diff_args = ["diff", "--stat", "HEAD"]
    return {
        "repo": str(repo), "author_emails": sorted(emails), "commits": commits,
        "working_tree": {
            "observed_at": datetime.now(config["tz"]).isoformat(timespec="seconds"),
            "historical": False,
            "status_porcelain": status.rstrip("\0").split("\0") if status else [],
            "diff_stat": git(repo, *diff_args, warnings=warnings) if status and not scoped else "",
        },
    }



def repo_relative(repo: Path, value: str) -> str | None:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return None
    return candidate.as_posix()



def excluded_git_path(repo: Path, relative_path: str, config: dict) -> bool:
    normalized = repo_relative(repo, relative_path)
    if normalized is None:
        return False
    return not allowed_path(str(repo / normalized), config)



def commit_is_excluded(repo: Path, commit_hash: str, config: dict, warnings: list[str]) -> bool:
    """提交涉及范围外路径时整条跳过，避免标题泄露被排除的工作。"""
    if not config["exclude_dirs"] and not config["include_dirs"]:
        return False
    warning_count = len(warnings)
    changed = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames",
                  "-r", "-z", commit_hash, warnings=warnings)
    if len(warnings) != warning_count:
        return True
    return any(excluded_git_path(repo, path, config) for path in changed.split("\0") if path)



def filtered_status(repo: Path, config: dict, warnings: list[str]) -> str:
    raw = git(repo, "status", "--porcelain=v1", "-z", warnings=warnings)
    if not raw:
        return ""
    # Git 的 -z 状态为重命名、复制额外输出一个路径，必须一起过滤。
    tokens = raw.rstrip("\0").split("\0")
    kept = []
    index = 0
    while index < len(tokens):
        record = tokens[index]
        path = record[3:] if len(record) >= 3 else ""
        paths = [path]
        if any(code in record[:2] for code in ("R", "C")) and index + 1 < len(tokens):
            paths.append(tokens[index + 1])
        if not any(excluded_git_path(repo, item, config) for item in paths):
            kept.append(record)
            if len(paths) == 2:
                kept.append(tokens[index + 1])
        index += len(paths)
    return "\0".join(kept) + ("\0" if kept else "")

