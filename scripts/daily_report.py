#!/usr/bin/env python3
"""daily-work-report 的命令行入口。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from zoneinfo import ZoneInfoNotFoundError

from collection import build_payload
from config_settings import DEFAULTS, config_file, expand_path, load_config, normalize_settings, update_config
from report_storage import archive_evidence, evidence_output_dir, report_paths, report_status, save_report


def main() -> int:
    # 入口只编排命令；采集、配置和保存规则由对应模块维护。
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Collect evidence without calling a model")
    collect.add_argument("--date")
    collect.add_argument("--exclude-session", action="append", default=[])
    collect.add_argument("--print", dest="print_payload", action="store_true")
    save = commands.add_parser("save", help="Save Markdown; preserve existing reports by default")
    save.add_argument("--evidence", required=True)
    save.add_argument("--candidate", required=True)
    save.add_argument("--replace", action="store_true")
    settings = commands.add_parser("config", help="Read or update persistent preferences")
    settings.add_argument("--config")
    settings.add_argument("--set", dest="changes")
    for command in (collect, save):
        command.add_argument("--config")
        command.add_argument("--output-dir")
        command.add_argument("--mode", choices=("brief", "detailed"))
    args = parser.parse_args()
    try:
        if args.command == "config":
            path = update_config(args.config, json.loads(args.changes)) if args.changes else config_file(args.config)
            if path.exists():
                load_config(str(path))
            values = normalize_settings(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
            print(json.dumps({"config_file": str(path), "settings": {**DEFAULTS, **values}}, ensure_ascii=False))
            return 0
        if args.command == "save":
            # 默认沿用证据所在目录和篇幅，避免切换工作目录后把日报存错位置。
            evidence_path = expand_path(args.evidence)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict):
                raise ValueError("Evidence must be a JSON object")
            candidate = expand_path(args.candidate).read_text(encoding="utf-8")
            output_dir = load_config(args.config, args.output_dir)["output_dir"] if (args.config or args.output_dir) else evidence_output_dir(evidence_path)
            mode = args.mode
            if mode is None:
                mode = evidence.get("report_mode")
                if mode is None:
                    mode = load_config(args.config)["style"]
            print(json.dumps(save_report(output_dir, evidence, candidate, mode, args.replace), ensure_ascii=False))
            return 0
        config = load_config(args.config, args.output_dir)
        mode = args.mode or config["style"]
        payload = build_payload(config, args.date, args.exclude_session)
        payload["report_mode"] = mode
        # 预览不落盘，便于核对采集范围。
        if args.print_payload:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        evidence_file = archive_evidence(config["output_dir"], payload)
        print(json.dumps({
            "evidence_file": str(evidence_file),
            "evidence_digest": payload["evidence_digest"],
            "snapshot_digest": payload["snapshot_digest"],
            "report_file": str(report_paths(config["output_dir"], payload["date"], mode)[0]),
            "report_status": report_status(config["output_dir"], payload["date"], mode, payload["evidence_digest"]),
            "report_mode": mode,
            "has_evidence": payload["has_evidence"],
            "sources": payload["sources"],
            "counts": payload["counts"],
            "warnings": payload["warnings"],
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ZoneInfoNotFoundError, re.error) as error:
        print(f"daily-work-report: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
