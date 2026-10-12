# Obsidian Worklog

**让 AI 帮你记工作、管项目、写周报——数据全部留在你自己的 Obsidian 知识库里。**

Obsidian Worklog 是一套“AI 工作记录与周报系统”：一个可被 Claude Code、Codex、WorkBuddy 等 AI 工具加载的 Skill（`obsidian-worklog`），加上一套本地 Python 命令行后端。你用自然语言告诉 AI“记一下今天做了什么”“继续某个项目”“出本周周报”，AI 通过统一命令读写你本地 Vault 中的项目台账、任务台账和工作节点；Obsidian 负责浏览，后端负责保证数据结构、修订历史和周报来源始终可靠。

> 仓库只包含程序、操作规则、配置示例和空白 Excel 模板；工作记录、真实配置和导出文件由使用者保存在自己的电脑上。

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心概念](#核心概念)
- [功能一览](#功能一览)
- [日常可以这样跟 AI 说](#日常可以这样跟-ai-说)
- [工作原理](#工作原理)
- [安装与配置](#安装与配置)
- [命令速查](#命令速查)
- [数据安全与可回滚](#数据安全与可回滚)
- [版本记录](#版本记录)
- [适用范围与限制](#适用范围与限制)

## 它解决什么问题

| 常见痛点 | Obsidian Worklog 的做法 |
|---|---|
| 工作散落在聊天、文档和脑子里，写周报时要翻半天 | 每次进展都以“工作节点”落到对应项目和任务下，周报按日期直接汇总来源 |
| 换一个对话、换一个 AI 工具，项目背景就要重新讲一遍 | 任何支持 Skill 的 AI 工具都读同一个 Vault：`brief` 看全局，`context` 恢复单个项目的状态、进展和下一步 |
| AI 记错、记重、把私人内容挂进业务项目 | 写入前自动预检，可疑记录先返回“需确认”；记错可一句话撤销，原文保留在审计历史中 |
| 灵感和未确认的事项不知道放哪 | 先放进收件箱，确认后再分流为工作记录、方法候选或个人笔记 |
| 做过的项目经验下次用不上 | 问题卡、方法候选和正式方法库形成经验链路；阶段收尾可一键汇总复盘证据 |
| 手写周报口径不一、容易漏项 | 周报草稿自动关联每条正文的来源节点，导出前校验覆盖完整、无内部路径编号、单元格不超长 |

## 核心概念

数据全部是 Vault 中的普通 Markdown（带 YAML frontmatter），可以直接在 Obsidian 中浏览、链接和搜索。

| 概念 | 说明 | 位置 |
|---|---|---|
| **项目台账** | 每个项目或持续工作一条：状态、分类、最新进展、下一步 | `周报系统/项目台账/` |
| **任务台账** | 项目下的任务与工作线（主线 / 支线 / 子任务），含状态历史 | `周报系统/任务台账/` |
| **工作节点** | 每一次具体进展、成果、反馈或状态变化，带发生时间、来源工具和关系 | `周报系统/工作节点/年/月/` |
| **修订链** | 更正历史不覆盖原文，而是新建修订节点（`supersedes`）；误记录只做“排除”标记，不删除 | 节点 frontmatter |
| **项目总览与关系图** | 每个项目的总览页（系统受控区自动同步）和 Canvas 工作关系图 | `项目管理/`、`周报系统/项目关系图/` |
| **工作全景** | 首页与 Obsidian Bases 视图：本月工作、工作日历、进行中项目、待办与等待、成果索引 | `00-工作全景/` |
| **问题卡与方法库** | 项目中的问题与决策；可复用做法先成为“候选”，经确认后进入正式方法库 | 项目目录、`方法论库/` |
| **收件箱** | 尚未确认或归属不清的随手记，不进入项目、全景和周报 | `00-收件箱/` |
| **个人笔记** | 与业务无关的个人内容，默认不参与工作检索 | `个人/随手记/` |

工作分为三个领域：**项目管理**（有目标和结项边界）、**培训工作**（平台、培训实施、考试竞赛等持续工作）、**日常与专项工作**（单次或临时事项），可按自己的 Vault 调整。

## 功能一览

### 1. 快速恢复工作背景
- `brief`：约 4K 字符的最小概览——近 7 天活跃项目、其他进行中项目、等待/阻塞任务、待确认记录与收件箱数量。
- `context --project`：单个项目的任务、最近进展和工作线；默认精简并有字符上限，`--full` 返回总览正文、问题与方法列表。
- `projects`、`tasks`、`graph`：项目清单（默认只含可见项目）、任务状态、主线与支线关系。

### 2. 检索
- `search`：跨项目、任务、节点、问题卡、方法和普通页面检索；默认跳过 `个人/` 等配置目录，`--scope all` 全库、`--scope personal` 只查个人笔记、`--business-only` 只查业务实体。

### 3. 记录工作（带写前预检）
- `record-intent-json`：AI 将“更新任务 / 新任务 / 支线 / 成果 / 状态变化 / 单次事项”意图交给后端，后端自动定位或创建任务、写入节点、同步项目总览、视图和关系图。
- **写前预检**：疑似重复、占位内容、“先记录之后修复”的需求池事项、内部分类调整、个人或机制内容、未来时间、等待中却写成催办、项目名多义——命中时返回 `needs_confirmation` 且不写入，经用户确认后带 `acknowledged_flags` 重新提交。
- `--dry-run` 预演写入范围；`schema` 输出输入格式与样例；`idempotency_key` 保证重试不重复写入。
- `undo`：一句话排除误记录（`--node-id` 或 `--last`），自动取哈希，保留审计历史。

### 4. 收件箱与个人笔记
- `capture`：先记下，不进项目和周报；`inbox` 查看待处理条目。
- `triage`：确认后分流为工作节点（同样经过预检）、方法候选、个人笔记，或说明理由后丢弃；文件不删除。
- `capture --personal`：个人内容直接写入 `个人/随手记/`，只在 `search --scope personal` 中出现。

### 5. 周报
- `preview`：按日期汇总本期进展与下期计划，`--raw` 返回完整来源与指纹。
- `preview --draft-review`：生成“来源已关联”的整理稿草稿，并标出需合并、需改写、超长的条目；`export_ready` 预先告诉你能否导出。
- `save-review-json` / `export`：校验来源覆盖与指纹后导出 Excel，并生成逐项核验记录；原记录不因周报整理而改写。

### 6. 经验沉淀与复盘
- `record-issue-json` / `resolve-issue-json`：问题与决策卡。
- `propose-method-json` → `confirm-method`：方法候选经确认进入正式方法库，`revise-method-json` 按哈希受控修订。
- `retro`：只读汇总一条工作线的时间线、反馈与阻塞信号、相关问题卡，生成方法候选草稿（默认最近 30 条）。

### 7. 看板
- `dashboard`：导出 `dashboard.json` 与自包含静态 `dashboard.html`（计数、进行中项目、等待与阻塞、本周进展、收件箱），默认写到 Vault 之外。

### 8. 维护与一致性
- `audit-integrity`、`audit-structure`：只读检查重复 ID、断链、派生字段、目录占用等。
- `repair-integrity`、`merge-projects`、`merge-tasks`、`move-task`、`move-project`、`rename-project`：先出计划、经批准后带哈希执行，失败自动恢复。
- `close-project`：生成复盘并结项；存在未完成任务时须明确允许。
- `worklog_node_migration.py`：节点字段迁移工具，plan → apply（先备份）→ rollback。

## 日常可以这样跟 AI 说

| 你说 | AI 实际做的事 |
|---|---|
| “我最近在忙什么？” | `brief`，再按需 `context` |
| “继续某某项目” | `context --project`，核对最新决定、版本和下一步，检索相关经验 |
| “记一下：今天第二期培训开展了，观看 160 人” | `record-intent-json`；预检有疑问会先问你 |
| “刚才那条记错了，撤掉” | `undo --last`（同一时间有多条时让你指定） |
| “先放收件箱：培训日历也许可以自动生成” | `capture` |
| “处理一下收件箱” | `inbox` → 逐条 `triage` |
| “出本周周报” | `preview --draft-review` → 合并改写、核验事实 → `export` |
| “这个阶段复盘一下” | `retro` → 提炼结论 → 你确认后 `propose-method-json` |
| “生成看板” | `dashboard` |
| “检查一下数据有没有问题” | `audit-integrity`、`audit-structure` |

## 工作原理

```text
你（自然语言）
   │
   ▼
AI 工具（Claude Code / Codex / WorkBuddy / 豆包 / 千问办公 等）
   │  读取 skills/obsidian-worklog/SKILL.md：何时查询、何时写入、哪些操作必须先确认
   ▼
scripts/record_worklog.py  ── 统一入口，自动使用后端虚拟环境
   │
   ▼
backend/worklog_cli.py  ── 命令解析、写前预检、只读 / 写入边界
   │
   ├─ worklog_core      项目、任务、节点与记录（含跨进程写锁）
   ├─ worklog_guard     写前预检、dry-run、输入 Schema
   ├─ worklog_search    检索            ├─ worklog_views      Bases、Canvas、工作全景
   ├─ worklog_knowledge 问题与方法      ├─ worklog_materials  项目资料索引
   ├─ worklog_inbox     收件箱与个人笔记 ├─ worklog_integrity  审计与修复
   ├─ reporting / report_draft / weekly_export   周报预览、草稿与 Excel
   └─ worklog_dashboard / worklog_retro          看板与复盘草稿
   │
   ▼
你的 Obsidian Vault（纯 Markdown + YAML frontmatter + Bases + Canvas）
```

几条设计原则：

- **Obsidian 是唯一事实来源**：AI 不直接手改台账、节点或自动生成的视图，一律通过命令写入。
- **只记录已确认的业务事实**：草稿、推测、普通收藏和私人内容不进入业务项目。
- **历史不覆盖**：更正走修订链，误记录只排除不删除；发生时间与录入时间分开保存。
- **关键操作保留确认点**：历史排除、修复、归并、目录维护、带遗留事项结项和正式方法确认，都先给出具体范围再执行。
- **多个 AI 工具可同时使用**：写操作持有同一把 Vault 锁，查询不修改业务文件。

## 安装与配置

### 运行要求

- macOS 和 Python 3.12；Windows 原生运行尚未验证。
- 一个可读写的 Obsidian Vault（可先用空 Vault 试运行）。命令行操作时无需打开 Obsidian 客户端。
- Git；AI Skill 为可选入口，后端也可单独通过命令行使用。

### 安装后端

```bash
git clone https://github.com/NoNoliuliu/obsidian-worklog-kit.git
cd obsidian-worklog-kit
python3.12 -m venv backend/.venv-mac
backend/.venv-mac/bin/python -m pip install -r backend/requirements.txt
cp backend/config.example.yaml backend/config.yaml
```

### 配置 `backend/config.yaml`

| 配置项 | 作用 | 默认值 |
|---|---|---|
| `vault_root` | 你的 Obsidian Vault 路径 | `backend/vault` |
| `export_dir` | 周报 Excel 导出目录 | `backend/exports` |
| `weekly_source_dir` | 仅用于导入历史周报 | `backend/imports` |
| `template_path` | 周报 Excel 模板 | `backend/assets/weekly-template.xlsx` |
| `weekly_excluded_project_ids` | 周报固定排除的项目 ID（记录仍保留） | 空 |
| `search_exclude` | `search` 默认跳过的 Vault 目录 | `个人/`、`AI Skill仓库/` |
| `personal_prefixes` | `search --scope personal` 查询的目录 | `个人/` |
| `dashboard_dir` | 看板导出目录（必须在 Vault 之外） | `backend/runtime/dashboard` |

真实 `config.yaml`、Vault、导入和导出文件均被 `.gitignore` 排除，不要提交到版本库。首次使用空 Vault 时先创建最小目录：

```bash
mkdir -p backend/vault/周报系统/{项目台账,任务台账,工作节点}
```

### 安装 Skill

以 Codex 为例，从仓库根目录把 Skill 以符号链接装到个人 Skill 目录：

```bash
mkdir -p "$HOME/.codex/skills"
ln -s "$PWD/skills/obsidian-worklog" "$HOME/.codex/skills/obsidian-worklog"
```

其他 AI 工具同理，链接到各自的 Skill 目录（例如 Claude Code 的 `~/.claude/skills/`）。以符号链接安装时入口会自动找到同仓库的后端；若只能复制 Skill，请把环境变量 `WORKLOG_ROOT` 设为本仓库 `backend` 的绝对路径。目标位置已有同名 Skill 时先核对，不要直接覆盖。

支持的 AI 工具（`source` 字段会自动归一）：Claude Code、Codex、WorkBuddy、Doubao Work（豆包）、Qwen Office（千问办公）、QoderWork。

### 验证

```bash
python3 skills/obsidian-worklog/scripts/record_worklog.py --help
python3 skills/obsidian-worklog/scripts/record_worklog.py brief --pretty
```

返回 `ok: true` 且确认读到的是自己的 Vault 后，再让 AI 试运行，例如：

> 使用 obsidian-worklog，先读取 Skill 说明；只读列出我当前的项目，说明项目数和来源，不要写入。

随后在测试 Vault 中依次试一次：录入（可先 `--dry-run`）、预检拦截与确认后重提、`undo`、收件箱分流、`preview --draft-review` 与导出。详细验收步骤见 [`references/setup.md`](skills/obsidian-worklog/references/setup.md)，操作流程见 [`references/workflow.md`](skills/obsidian-worklog/references/workflow.md)。

## 命令速查

| 场景 | 命令 |
|---|---|
| 概览与查询 | `brief`、`projects`、`context`、`tasks`、`search`、`graph`、`node` |
| 记录与修订 | `record-intent-json`（`--dry-run`）、`revise-node-json`、`exclude-node-json`、`undo`、`task-update`、`update-project`、`add-project`、`schema` |
| 收件箱 | `capture`（`--personal`）、`inbox`、`triage` |
| 周报 | `preview`（`--raw`、`--draft-review`）、`save-review-json`、`export` |
| 经验 | `record-issue-json`、`resolve-issue-json`、`propose-method-json`、`confirm-method`、`method`、`list-candidates`、`revise-method-json`、`retro` |
| 看板 | `dashboard` |
| 维护 | `audit-integrity`、`audit-structure`、`repair-integrity`、`merge-projects`、`merge-tasks`、`move-task`、`move-project`、`rename-project`、`apply-project-maintenance`、`recover-project-maintenance`、`close-project` |
| 资料索引 | `setup-project-experience`、`index-project-materials`、`audit-project-materials` |

所有命令输出 JSON；查询命令不修改业务文件，加 `--timing` 可查看耗时。完整参数以 `--help` 为准。

## 数据安全与可回滚

- **全部本地**：数据是你 Vault 中的 Markdown 文件，后端不联网、不上传。
- **写前检查**：可疑写入先返回需确认；写操作持有跨进程锁，避免多个 AI 工具同时写入时丢失更新。
- **不删除历史**：更正保留修订链，误记录只标记排除；收件箱分流、目录清理都只移动或标记，不删除文件。
- **先备份再修改**：修复、归并、目录维护、节点迁移都先备份到 `周报系统/备份/`，可按清单回滚。
- **周报可追溯**：每份 Excel 附带 `.verification.json`，记录来源指纹与逐项整理依据。
- **结构检查不等于事实正确**：`ok: true` 和审计通过只代表结构一致，自然语言内容仍需人工核对。

## 版本记录

| 版本 | 主要变化 |
|---|---|
| 1.36 | 收件箱与个人笔记、周报整理稿草稿（`--draft-review`、`export_ready`）、看板导出、阶段复盘草稿；`undo --last` 并列保护 |
| 1.35 | 后端按职责拆分为多个模块；节点不再冗余保存项目级字段，并提供可回滚的迁移工具 |
| 1.34 | 写前预检与确认、`--dry-run`、`undo`、输入 Schema |
| 1.33 | `brief`；`context` 默认精简限长；检索提速并默认排除个人目录；停用批量关闭任务的旧命令 |
| 1.32 | 记录来源工具校验；周报排除名单改为配置项 |

## 适用范围与限制

- 主要在 macOS + Python 3.12 上使用和验证；Windows 原生运行未验证。
- 默认分类和领域是示例规则，可按自己的 Vault 结构调整后端配置与代码。
- 预检是基于规则的兜底，不能替代“是否应该记录”的判断；周报草稿只省去来源关联，事实核验和措辞仍需人工完成。
- 正式数据迁移前，请先在测试 Vault 中验证。
