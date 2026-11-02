# daily-work-report

从本机 Codex、Claude Code 会话与 Git 活动，生成跨项目工作日报。会话还原做过的事，Git 核对提交；已有日报有更新时另存草稿，保留人工修改。

需要 Python 3.10+、Git 和支持 skill 的编码 Agent，无需安装 Python 第三方库或额外配置模型 Key。已在 Linux / WSL 验证，其他平台待验证。

## 安装

在终端执行：

```bash
git clone https://github.com/btnkr/daily-work-report.git ~/.agents/skills/daily-work-report
```

这是 Codex 当前官方文档列出的用户级 skills 目录。也可以在 Codex 中让内置 `$skill-installer` 从本仓库安装根目录的 skill。Claude Code 则克隆到 `~/.claude/skills/daily-work-report`。

## 使用

在编码 Agent 的对话中输入：

```text
使用 daily-work-report 生成昨天的工作日报。
```

默认写简短日报，保存在当前目录的 `daily-work-reports/`。可以指定日期、输出目录，或要求“详细复盘版”。首次使用无需配置。

示例内容（虚构）：

```markdown
# 2026-10-20 工作日报

- 订单筛选：完成两个项目的调整并提交，会话记录本地回归通过，待跨端联调。
- 对账导出：确认字段与空值口径，形成实现方案。

来源：Codex 2 条会话；本地证据 _raw/2026-10-20.json。
```

默认只核对会话关联的仓库。需要额外扫描仓库、排除目录或修改时区时，见 [配置与命令](references/data-and-config.md)。

## 工具兼容

自动采集历史记录目前仅支持 Codex 和 Claude Code。Cursor、Qoder 等工具即使能加载本 skill，也不会因此自动读取它们自己的历史；其日志适配仍待实现。统一工作记录导入是后续方向，当前版本尚未提供。

Skill 加载方式可参考 [Cursor](https://cursor.com/docs/context/skills)、[Qoder](https://docs.qoder.com/extensions/skills.md) 各自文档。Windsurf、TRAE 的安装方式和历史采集也需要按具体版本验证。

## 数据说明

脚本只在本机采集和保存，正文由当前 Agent 生成；远程模型会通过该 Agent 接收所读素材。脚本会过滤系统注入并对部分常见凭证脱敏，分享日报前仍需检查正文。

只统计可访问的文字会话和本地 Git，不代表完整工作记录。大量历史日志可能较慢，日志版本变化也可能影响解析；Claude Code 目前有合成日志测试，尚缺真实端到端验收。

## 开发

一个脚本负责采集与保存，`SKILL.md` 负责写作判断。回归测试使用合成日志和临时 Git 仓库：

```bash
python3 -m unittest discover -s tests -v
```

MIT，见 [LICENSE](LICENSE)。安装路径依据 [OpenAI 官方 Skills 文档](https://developers.openai.com/codex/skills/)。
