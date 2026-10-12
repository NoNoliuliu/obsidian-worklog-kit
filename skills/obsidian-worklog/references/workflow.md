# 操作流程

## 1. 查询项目背景

先运行 `brief` 获取近期项目、其他进行中项目、等待/阻塞任务和收件箱数量；需要完整清单时用 `projects --compact --sort-by last_activity`。再按项目名运行 `context --project "项目名"`（默认精简并有字符上限，`--full` 返回总览正文、问题和方法列表），按需补查 `tasks`、`search`、`graph`。重名时先区分；状态、当前进展和下一步以最新正式字段为准，缺失事实标记待确认。

## 2. 归档与修订

判断内容属于业务项目、单次事项、工作机制还是个人内容。核对已有项目与任务，提炼“做了什么、结果、下一步、发生日期”，再调用兼容后端的录入命令，例如 `record-intent-json`。`source` 填执行记录的 AI 工具：WorkBuddy、Claude Code、Codex、Doubao Work、Qwen Office 或 QoderWork；“豆包”“千问办公”等简称会自动归一，其他名称会被拒绝。写前预检命中时返回 `needs_confirmation` 且不写入：说明 `findings`，经用户确认后加 `acknowledged_flags` 重提。范围不确定时先 `--dry-run`。更正历史使用 `revise-node-json` 和当前哈希，保留原文；刚写错的记录用 `undo --node-id … --reason …`（或 `--last`，同一时间多条时须改用 `--node-id`）。写后复查受影响任务、项目总览及视图；出现警告时先查原因，不盲目重试。

## 收件箱与个人笔记

尚未确认或归属不清的内容用 `capture --text "…"` 写入 `00-收件箱/`，不进入项目、全景和周报；`inbox` 列出待处理条目。用户确认后 `triage --inbox-id … --to node|candidate|personal|discard`：node 从标准输入提供 record-intent JSON（同样预检），candidate 提供 propose-method JSON，discard 须 `--reason`。`capture --personal` 直接写入 `个人/随手记/`，只在 `search --scope personal` 中出现。

## 3. 经验复用

读取最新背景 → 按任务或症状检索历史问题与方法 → 阅读适用条件和证据 → 在本次方案中采用可用检查点 → 验证结果 → 记录新增例外或改进。命中历史经验只是线索，不证明本次根因。正式方法候选在用户批准具体版本后确认。阶段收尾可先运行只读 `retro --project "项目名" [--lane …] [--since …] [--limit 30]`，获得时间线、反馈/阻塞信号、问题卡和 propose-method-json 格式草稿。

## 4. 周报

用 `preview --start YYYY-MM-DD --end YYYY-MM-DD --raw` 获取完整候选与来源，`--draft-review` 获取已关联来源的整理稿草稿（`draft_notes` 标出需合并、改写、超长的条目，`export_ready` 表示格式可导出）。逐项核对期间事实、并行事项、计划、统计口径与遗漏；以结果语言撰写，去掉路径、内部 ID、邮件和日志。按后端要求保留来源映射与摘要指纹，再导出 Excel；复查文件、周期、行数和内容。周报只是 Vault 中已确认事实的期间呈现。需要固定排除的项目，在 `config.yaml` 的 `weekly_excluded_project_ids` 中按项目 ID 配置。

## 5. 维护与结项

`audit-integrity`、`audit-structure` 先只读检查。对修复、历史排除、项目归并、目录搬移、改名或恢复，先列出对象、计划和影响，取得批准后按后端的哈希、锁及备份流程执行。普通更新使用项目或任务更新命令。项目结项使用 `close-project`；有未完成任务或问题时先列明遗留项，并在明确批准后才带遗留结项。最后复查项目、任务、索引、视图和相关周报预览。
