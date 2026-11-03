# 配置与命令

供执行 skill 的 Agent 按需读取：配置字段或采集结果不明确、需要回查证据时使用。

## 环境处理

安装、首次运行或命令报错时，确认 Python 3.10+、Git 和配置时区可用。选择当前宿主已有的解释器，例如 Windows 的 `python`、`py -3` 或 POSIX 环境的 `python3`，不要固定使用某个系统路径。以下命令中的 `python3` 仅作示例。

优先使用当前打开工作区绑定的 Python，但先确认它能在当前终端执行、满足版本要求并访问会话数据和输出目录。Windows 与 WSL 属于不同运行环境，不混用两侧的解释器和路径；没有合适的工作区解释器时，使用当前宿主可用的 Python。

首次安装或环境报错时，运行同目录的 `verify_env.py --timezone <实际配置时区>`。默认检查 `Asia/Shanghai`，输出 JSON 包含 Python 版本、Git、SQLite、时区和平台信息；退出码 0 表示依赖就绪，1 表示有缺项，按 `errors` 定位。脚本只读检查，不安装依赖。正常生成无需每次重复检查。

缺少依赖时由 Agent 在用户已授权的安装范围内处理，遇到实际权限限制使用宿主的授权流程，不要求用户编辑脚本或手动试命令。时区名称正确而系统缺少数据时，用同一解释器安装 `tzdata` 后复查；无法完成时说明具体原因，不改用错误时区。正常使用不重复安装、不覆盖已有个人配置，采集脚本自身不联网安装依赖。

## 命令

以下命令中的脚本路径应替换为 skill 所在目录的绝对路径，解释器使用当前环境已检查可用的 Python。

```bash
python3 scripts/daily_report.py collect --date 2026-10-20
python3 scripts/daily_report.py save --evidence <collect返回的evidence_file> --candidate <本次候选文件>
```

`collect` 的 `--print` 只打印证据、不写文件；`--exclude-session ID` 可重复使用。默认按 `style` 选择篇幅，`--mode detailed` 临时覆盖。保存自动沿用采集记录的篇幅与证据目录；`--replace` 用于明确覆盖已有日报。

以采集结果中的实际 `evidence_file`、`report_file`、`report_mode` 为准，不根据命令示例拼接文件路径。缺省时区为 `Asia/Shanghai`，输出目录为 `~/daily-work-reports/`；解释“今天”“昨天”时按实际配置计算。

两命令均支持 `--output-dir` 和 `--config`。采集时目录优先级：命令行 > 配置 > `~/daily-work-reports/`；保存时默认找到证据路径中的 `_raw` 并使用其父目录，显式参数可覆盖。

用 `python3 scripts/daily_report.py config` 查看设置；持久修改使用 `config --set '{"style":"detailed"}'`，只更新指定字段并保留其他配置。修改对象映射时先读取并合并已有项；无效配置不会覆盖原文件。临时覆盖不会修改配置。

## 配置

默认读取 `~/.config/daily-work-report/config.json`，也可用 `DAILY_WORK_REPORT_CONFIG` 或 `--config` 指定。仅需填写要修改的项，字段见下表。路径支持 `~`、环境变量，相对路径按运行目录解析。

最小配置示例，可直接作为 `config --set` 的 JSON 变更：

```json
{
  "output_dir": "~/daily-work-reports",
  "style": "brief",
  "repositories": ["~/work/orders"]
}
```

`~` 始终表示执行 Python 的当前用户主目录。Windows 原生 Python、WSL 内 Python 各有自己的主目录与配置；同一台电脑中也不会自动共用。

| 字段 | 默认与作用 |
| --- | --- |
| `output_dir` | `~/daily-work-reports`，固定保存证据、正文和生成状态 |
| `style` | `brief`，可设为 `detailed` |
| `timezone` | `Asia/Shanghai`，按本地午夜至次日午夜筛选，支持夏令时 |
| `sources` | `{}`，工具名到目录列表的映射；各工具未配置或列表为空时自动发现 |
| `project_roots` | `[]`，额外扫描这些目录下没有会话的 Git 仓库 |
| `repositories` | `[]`，明确列出的仓库路径，直接读取，不递归扫描 |
| `include_dirs` / `exclude_dirs` | `[]`，纳入或排除目录及其后代，排除优先 |
| `exclude_session_ids` | `[]`，忽略指定会话 |
| `project_names` | `{}`，绝对目录路径到展示名的映射 |
| `author_emails` | `[]`，默认读取各仓库生效的邮箱；配置后精确匹配这些邮箱 |
| `redact_patterns` | `[]`，追加 Python 正则脱敏规则 |
| `max_text_chars` / `scan_depth` | `3000` / `4`，单条消息长度和额外仓库扫描深度 |

`repositories` 与 `project_roots`、会话关联仓库和当前工作区合并去重，不作为独占白名单；纳入和排除目录规则对所有来源生效。清单中的路径不可访问或不是 Git 仓库时，采集结果会说明该项未能读取，其他仓库继续采集。

Git 提交涉及任一排除路径时，保守跳过整条提交，避免提交标题带出排除内容。工作区状态逐条过滤，重命名的任一路径被排除时整条过滤；存在范围过滤时不输出整仓 diff 统计。

### 会话数据目录

统一使用 `sources.<工具名>` 配置目录列表：

| 配置项 | 目录内容 | 自动发现 |
| --- | --- | --- |
| `sources.codex` | 含 `sessions/` 或 `archived_sessions/` 的数据根目录 | `CODEX_HOME` 或 `~/.codex` |
| `sources.claude` | 含 `projects/` 或 `history.jsonl` 的数据根目录 | `~/.claude` |
| `sources.cursor` | 含 `workspaceStorage/`、`globalStorage/` 的 User 目录 | 当前系统的应用数据目录 |

每项均可包含多个路径，显式路径替代该工具的自动发现。修改 `sources` 中某个工具时，会保留其他工具的配置。

### Cursor 数据位置

自动发现使用当前平台的系统目录，不依赖固定用户名：

| 运行环境 | 默认 User 数据目录 |
| --- | --- |
| Windows 原生 | `%APPDATA%/Cursor/User` |
| macOS | `~/Library/Application Support/Cursor/User` |
| Linux | `~/.config/Cursor/User`，设置了 `XDG_CONFIG_HOME` 时沿用该目录 |
| WSL | 先发现 Linux 侧目录，再尝试当前用户对应的 Windows 目录 |

以上是系统默认位置；使用自定义数据目录、不同 Windows/WSL 用户名或自定义挂载位置时，将 Python 实际可访问的 User 目录填入 `sources.cursor`。Windows 配置中的路径可使用正斜杠，或按 JSON 规则转义反斜杠。

Windows 盘符路径只在 WSL 中转换成其默认挂载路径，不在原生 Linux 或 macOS 中转换。远程工作区、非默认 WSL 挂载等情形可能无法自动核对 Git，不会据此猜测仓库。

## 证据与保存

`_raw/YYYY-MM-DD/<版本摘要>.json` 包含按项目归组的会话、提交及当前工作区快照。版本摘要基于整份素材（含快照和篇幅），排除仅采集时间的变化；它与判断日期素材变化的 `evidence_digest` 用途不同。新素材另存版本，相同版本复用，不覆盖已有文件。消息保留 `id`、`source_file`、`source_line` 和截断标记，Cursor 另有 `source_key`，可回查数据库中的键或内联数组位置。日期按消息时间和 Git committer timestamp 筛选；没有作者邮箱时跳过提交并告警。`git_only` 是无会话的提交项目，`unattached_sessions` 是无法归属的会话。

证据中的 `sources` 表示各工具的数据目录是否发现，与个人配置中用于指定目录的 `sources` 结构不同；不要把采集状态回写到配置。

`evidence_digest` 反映目标日期素材，`snapshot_digest` 单独反映当前工作区状态；当前快照变化不会触发历史日报重写。证据格式版本为 2，旧版本首次检查可能出现一次摘要变化。

采集返回 `evidence_file`、`report_file`、`report_mode`、`has_evidence`、来源发现状态与 `report_status`（`missing` / `new_evidence` / `up_to_date` / `edited` / `unmanaged`）。无素材时不生成日报。正文在 `daily/`，简短版后缀 `.brief.md`，详细版 `.md`。`_state/` 记录素材摘要、实际落盘正文摘要及对应版本文件的 `evidence_file`；未获覆盖授权时状态也保持不变。`save` 对不同候选返回 `review_required`，保留原文，不创建额外草稿文件。

`saved` 或 `unchanged` 表示成稿已在返回路径；`up_to_date` 表示采集摘要与已有正文状态一致，应展示该文件。不同候选一律返回 `review_required`，由 Agent 在对话中展示更新稿，用户明确覆盖后再传 `--replace`。用户模板、补充材料不会被采集摘要自动记录。

回查 JSONL 时只读取相关 `source_file`、`source_line` 周边；Cursor 使用 `source_key` 定位，避免将数据库当文本文件读取。默认摘要不需要公开完整来源，用户要求核对或详细复盘时再给相关证据。

## 解析边界

Codex 支持活动和归档 JSONL，合并重复表示；fork 开始两秒内的部分旧格式用户消息视作继承上下文，无法覆盖所有重放格式。Claude 优先读项目会话，缺正文时从 history 取提问，跳过 sidechain；混合目录会话只要出现排除目录就整条排除。这些规则不能证明所有工具访问均在纳入范围内。

只读取文字消息，不读取图片、完整工具输出或 raw diff；为支持历史补写，不按文件修改时间跳过旧日志，也未实现缓存。

Cursor IDE 读取 User 目录中 workspaceStorage 与 globalStorage 的 SQLite 数据库，使用只读连接，兼容 WAL。工作区的 `composer.composerData` 与 `workspace.json` 关联项目，全局 `composerData:` 与 `bubbleId:` 提取文字消息；存在会话头列表时只纳入该列表的消息。逐条消息必须有 `createdAt` 或 `timestamp`，不使用文件修改时间或会话更新时间推断日期。

数据库消息的 `source_key` 为 `bubbleId:会话:消息` 时，在 `cursorDiskKV` 精确查询该键；包含 `:conversation:索引` 时，先按前面的原键读取 JSON，再定位 conversation 数组。只读回查必要片段并继续脱敏。

