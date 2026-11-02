# 配置与命令

## 命令

```bash
python3 scripts/daily_report.py collect --date 2026-10-20
python3 scripts/daily_report.py save --evidence ./daily-work-reports/_raw/2026-10-20.json --candidate ./daily-work-reports/candidate.md
```

`collect` 的 `--print` 只打印证据、不写文件；`--exclude-session ID` 可重复使用。默认简短版，详细版的采集、保存均传 `--mode detailed`。`save` 自动沿用证据所在的输出目录，`--replace` 用于明确覆盖已有日报。

两命令均支持 `--output-dir` 和 `--config`。采集时目录优先级：命令行 > 配置 > 当前目录的 `daily-work-reports/`；保存时默认从 `_raw` 的父目录确定输出位置，显式参数可覆盖。

## 配置

默认读取 `~/.config/daily-work-report/config.json`，也可用 `DAILY_WORK_REPORT_CONFIG` 或 `--config` 指定。可参考仓库根目录的 `config.example.json`；仅需填写要修改的项。路径支持 `~`、环境变量，相对路径按运行目录解析。

| 字段 | 默认与作用 |
| --- | --- |
| `output_dir` | `./daily-work-reports`，保存证据、正文和生成状态 |
| `timezone` | `Asia/Shanghai`，按本地午夜至次日午夜筛选，支持夏令时 |
| `codex_home` / `claude_home` | `~/.codex` / `~/.claude`；Codex 默认识别 `CODEX_HOME` |
| `project_roots` | `[]`，额外扫描这些目录下没有会话的 Git 仓库 |
| `include_dirs` / `exclude_dirs` | `[]`，纳入或排除目录及其后代，排除优先 |
| `exclude_session_ids` | `[]`，忽略指定会话 |
| `project_names` | `{}`，绝对目录路径到展示名的映射 |
| `author_emails` | `[]`，默认读取各仓库生效的邮箱；配置后精确匹配这些邮箱 |
| `redact_patterns` | `[]`，追加 Python 正则脱敏规则 |
| `max_text_chars` / `scan_depth` | `3000` / `4`，单条消息长度和额外仓库扫描深度 |

## 证据与保存

`_raw/YYYY-MM-DD.json` 包含按项目归组的会话、提交及当前工作区快照。每条消息保留 `id`、`source_file`、`source_line` 和截断标记，可回查来源。日期按消息时间和 Git committer timestamp 筛选；没有作者邮箱时跳过提交并告警。`git_only` 是额外扫描到的纯提交项目，`unattached_sessions` 是无法归属的会话。

`evidence_digest` 反映目标日期素材，`snapshot_digest` 单独反映当前工作区状态；当前快照变化不会触发历史日报重写。证据格式版本为 2，旧版本首次检查可能出现一次摘要变化。

采集返回 `evidence_file`、`report_file` 和 `report_status`（`missing` / `new_evidence` / `up_to_date` / `edited` / `unmanaged`）。正文在 `daily/`，简短版后缀 `.brief.md`，详细版 `.md`；更新草稿带 `.draft-内容摘要.md`。`_state/` 记录素材与正文摘要，用于识别新材料和人工修改。

## 解析边界

Codex 支持活动和归档 JSONL，合并重复表示；fork 开始两秒内的部分旧格式用户消息视作继承上下文，无法覆盖所有重放格式。Claude 优先读项目会话，缺正文时从 history 取提问，跳过 sidechain；混合目录会话只要出现排除目录就整条排除。这些规则不能证明所有工具访问均在纳入范围内。

只读取文字消息，不读取图片、完整工具输出或 raw diff；为支持历史补写，不按文件修改时间跳过旧日志，也未实现缓存。

设计参考：[harness-daily](https://github.com/TardisBooo/harness-daily)、[ai-report-to-me](https://github.com/FakeHank/ai-report-to-me)、[codex-daily-report](https://github.com/jianzhangg/codex-daily-report)。
