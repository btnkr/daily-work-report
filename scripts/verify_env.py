#!/usr/bin/env python3
"""只读检查运行依赖，输出 JSON；不联网、不安装依赖、不读取会话。"""

import argparse
import json
import os
import shutil
import sys
from contextlib import closing


def check_env(timezone_name="Asia/Shanghai"):
    # 不导入业务模块，让依赖不完整或 Python 版本较旧时也能给出诊断。
    status = {
        "python_version": sys.version.split()[0],
        "python_supported": sys.version_info >= (3, 10),
        "git_available": shutil.which("git") is not None,
        "platform": sys.platform,
        "is_wsl": sys.platform.startswith("linux") and (
            bool(os.environ.get("WSL_DISTRO_NAME"))
            or (hasattr(os, "uname") and "microsoft" in os.uname().release.lower())
        ),
        "sqlite_available": False,
        "timezone": timezone_name,
        "timezone_available": False,
        "errors": [],
    }
    if not status["python_supported"]:
        status["errors"].append("需要 Python 3.10 或更高版本。")
    if not status["git_available"]:
        status["errors"].append("当前终端未找到 Git，请安装 Git 或检查 PATH。")

    try:
        import sqlite3
    except ImportError:
        status["errors"].append("当前 Python 缺少 SQLite 支持，请使用包含 sqlite3 的 Python。")
    else:
        try:
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("SELECT 1").fetchone()
            status["sqlite_available"] = True
        except sqlite3.Error as error:
            status["errors"].append("SQLite 无法使用：" + str(error))

    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        status["errors"].append("当前 Python 缺少 zoneinfo，请使用 Python 3.10 或更高版本。")
    else:
        try:
            ZoneInfo(timezone_name)
            status["timezone_available"] = True
        except (ZoneInfoNotFoundError, ValueError):
            status["errors"].append(
                "无法加载时区 " + timezone_name
                + "；请先确认时区名称，缺少数据时用当前解释器执行 -m pip install tzdata。"
            )

    status["ok"] = not status["errors"]
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timezone", default="Asia/Shanghai", help="要检查的 IANA 时区名称")
    args = parser.parse_args()
    status = check_env(args.timezone)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
