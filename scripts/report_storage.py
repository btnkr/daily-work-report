"""保存不可覆盖的证据版本，并保护已有日报与人工修改。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from config_settings import atomic_write
from evidence import fingerprint

def report_paths(output_dir: Path, date: str, mode: str) -> tuple[Path, Path]:
    if mode not in ("brief", "detailed"):
        raise ValueError("Report mode must be brief or detailed")
    suffix = ".brief" if mode == "brief" else ""
    return output_dir / "daily" / f"{date}{suffix}.md", output_dir / "_state" / f"{date}{suffix}.json"



def archive_evidence(output_dir: Path, evidence: dict) -> Path:
    """按内容版本归档；仅采集时间变化时复用已有证据。"""
    date = evidence["date"]
    datetime.strptime(date, "%Y-%m-%d")
    stable = json.loads(json.dumps(evidence))
    stable.pop("generated_at", None)
    git_records = [stream["git"] for stream in stable.get("workstreams", []) if stream.get("git")]
    for info in [*git_records, *stable.get("git_only", [])]:
        info.get("working_tree", {}).pop("observed_at", None)
    version = fingerprint(stable)
    target = output_dir / "_raw" / date / f"{version}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        pass
    return target



def evidence_output_dir(path: Path) -> Path:
    for parent in path.parents:
        if parent.name == "_raw":
            return parent.parent
    raise ValueError("Evidence must be inside an _raw directory, or specify --output-dir")



def report_status(output_dir: Path, date: str, mode: str, digest: str) -> str:
    # 先判断正文是否被人工修改，再判断采集素材是否更新。
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
    """候选与原文不同时先返回待审阅，只有明确覆盖才替换。"""
    date = evidence["date"]
    datetime.strptime(date, "%Y-%m-%d")
    if not candidate.strip():
        raise ValueError("Report cannot be empty")
    if evidence.get("has_evidence") is False:
        raise ValueError("No dated evidence; report was not saved")
    target, state = report_paths(output_dir, date, mode)
    content = candidate.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    status = "saved"
    if target.exists() and not replace:
        if target.read_text(encoding="utf-8") == content:
            status = "unchanged"
        else:
            return {"status": "review_required", "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    evidence_file = archive_evidence(output_dir, evidence)
    if status == "saved":
        if replace:
            atomic_write(target, content)
        else:
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    # 摘要以实际落盘字节为准，状态同时指向对应的证据版本。
    atomic_write(state, json.dumps({"evidence_digest": evidence["evidence_digest"], "report_digest": digest,
                                   "evidence_file": str(evidence_file.resolve())},
                                   indent=2) + "\n")
    return {"status": status, "path": str(target)}

