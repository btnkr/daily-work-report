# daily-work-report

**daily-work-report** 是一个面向 AI 编程工作流的开源 Skill。它读取本机 AI 编程会话和 Git 活动，由你正在使用的 Agent 生成跨项目工作日报。

会话和 Git 证据在本机采集；日报由当前 Agent 生成。无需为这个 Skill 单独配置模型 Key。

## 支持环境

- Windows、macOS、Linux（含 WSL）
- Python 3.10+
- Git
- Codex、Claude Code
- Cursor IDE Composer

## 安装

把 GitHub 地址交给正在使用的编码 Agent：

```text
请从 https://github.com/btnkr/daily-work-report 安装 daily-work-report skill。
```

安装时只需要将下面三个内容放入对应工具的 skills 目录：

```text
SKILL.md
scripts/
references/
```

常见用户级目录：

- Codex：`~/.agents/skills/daily-work-report/`
- Claude Code：`~/.claude/skills/daily-work-report/`
- Cursor：`~/.cursor/skills/daily-work-report/`

也可以下载仓库后手动复制。完整仓库包含源码和配置示例；日常使用只需要上面的三个运行内容。

## 使用

安装后，在 Agent 对话中说：

```text
使用 daily-work-report 生成昨天的工作日报。
```

也可以指定日期、篇幅或本次输出位置：

```text
生成 2026-10-20 的详细复盘版日报。
```

日报正文会直接展示在对话中，并默认保存到：

```text
~/daily-work-reports/
```

已有日报需要更新时，先展示更新稿；只有你明确要求覆盖，才会替换原文。

## 你可以配置什么

长期偏好保存在：

```text
~/.config/daily-work-report/config.json
```

不需要手动编辑，可以直接告诉 Agent：

```text
以后日报保存到 ~/work-reports。
这个项目叫订单后台。
个人项目不要写进日报。
以后默认写详细复盘。
```

常用配置包括：

- `output_dir`：日报和采集证据的保存目录
- `style`：`brief` 或 `detailed`
- `timezone`：日报日期使用的时区
- `sources`：Codex、Claude Code、Cursor 的会话数据目录
- `repositories`：逐个维护的 Git 仓库路径
- `project_roots`：自动扫描仓库的父目录
- `exclude_dirs`：排除目录
- `project_names`：项目展示名称
- `author_emails`：Git 提交作者邮箱

详细字段和平台路径见 [配置参考](references/data-and-config.md)。

## 如何确定项目

工具会合并三类信息：

1. AI 会话记录中的工作目录；
2. 当前工作区所属的 Git 仓库；
3. 配置中的 `repositories` 和 `project_roots`。

会话中的工作目录会通过 Git 根目录归属到项目；同一件事跨多个仓库时，日报可以合并叙述。无法确定归属的会话会单独保留，不强行归入某个项目。

## 隐私

会话和 Git 记录由脚本在本机读取。日报由你正在使用的 Agent 生成；如果 Agent 使用远程模型，整理后的素材会发送给对应服务商。

工具会对部分常见凭证做脱敏，但分享日报前仍应检查内容。

## 许可证

[MIT](LICENSE)
