# Obsidian Worklog

一套以本地 Obsidian Vault 为数据源的工作记录工具，包含 Python 命令行后端和可供 AI 工具加载的 `obsidian-worklog` Skill。它用于查询项目背景、记录进展、保留修订、复用经验，以及按日期预览和导出周报。

仓库只包含程序、操作规则、配置示例和空白 Excel 模板。工作记录、真实配置和导出文件由使用者保存在自己的电脑上。

## 功能

- **项目上下文**：查询项目、任务、节点与关系，恢复当前状态和下一步。
- **工作归档**：按已确认的事实记录进展、成果与状态变化；更正保留修订链。
- **经验复用**：检索问题和方法，核对适用条件，记录验证结果。
- **周报**：按日期预览完整来源，人工核对内容后导出 Excel。
- **维护**：只读审计结构与关联；修复、归并和目录调整按受控流程执行。

## 运行要求

- macOS 和 Python 3.12；Windows 原生运行尚未验证。
- 一个可读写的 Obsidian Vault；可以先使用空 Vault 测试。命令行操作时无需打开 Obsidian 客户端。
- Git 用于获取和更新代码；AI Skill 为可选入口，后端也可独立通过命令行使用。

## 安装

```bash
git clone https://github.com/NoNoliuliu/obsidian-worklog-kit.git
cd obsidian-worklog-kit
python3.12 -m venv backend/.venv-mac
backend/.venv-mac/bin/python -m pip install -r backend/requirements.txt
cp backend/config.example.yaml backend/config.yaml
```

默认配置使用仓库内的 `backend/vault`、`backend/imports` 和 `backend/exports`。如需使用已有 Vault，请在 `backend/config.yaml` 中设置 `vault_root` 等路径。首次使用空 Vault 时，创建最小目录：

```bash
mkdir -p backend/vault/周报系统/{项目台账,任务台账,工作节点}
```

真实 `config.yaml`、Vault、导入文件和导出文件均被 `.gitignore` 排除。不要将其手工提交到版本库。

## 命令行使用

从仓库根目录运行：

```bash
python3 skills/obsidian-worklog/scripts/record_worklog.py projects --compact --pretty --visible-only --sort-by last_activity
python3 skills/obsidian-worklog/scripts/record_worklog.py context --project "项目名"
python3 skills/obsidian-worklog/scripts/record_worklog.py preview --start 2026-09-21 --end 2026-09-25
python3 skills/obsidian-worklog/scripts/record_worklog.py audit-integrity
```

使用 `--help` 查看命令和参数。首次运行建议先只读查询，确认返回的是自己的 Vault，再在测试数据上尝试写入和导出。`preview` 是周报候选内容；正式导出前仍需核对事实、统计口径和来源。

## 在 AI 工具中使用

支持本地 Skill 的工具可以加载 `skills/obsidian-worklog`。例如，在 Codex 的 macOS 个人 Skill 目录中创建符号链接（从仓库根目录运行）：

```bash
mkdir -p "$HOME/.codex/skills"
ln -s "$PWD/skills/obsidian-worklog" "$HOME/.codex/skills/obsidian-worklog"
```

如果目标位置已有同名 Skill，先核对已有内容，不直接覆盖。安装后重新加载 Skill。可用以下请求检查是否正常：

> 使用 obsidian-worklog，先读取 Skill 说明，只读列出当前项目并说明数据来源，不要写入。

以符号链接安装时，入口会自动找到同仓库的后端。如果复制 Skill 到其他目录，需将环境变量 `WORKLOG_ROOT` 设为 `backend` 的绝对路径，并确保 AI 工具运行进程能读取它。流程和操作边界见 [`skills/obsidian-worklog/SKILL.md`](skills/obsidian-worklog/SKILL.md)。

## 数据与适配边界

代码不附带项目数据，也不会连接任何现成的工作记录。默认分类是示例规则，可按自己的 Vault 结构调整。当前验证覆盖空白测试 Vault 的项目创建、查询、周报预览和 Excel 导出；正式数据迁移、全部维护命令及 Windows 环境未做完整验收。
