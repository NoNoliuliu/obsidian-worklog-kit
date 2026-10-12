---
name: obsidian-worklog
description: 使用 Obsidian 工作记录后端查询项目背景、归档已确认的进展和任务、复用经验，并预览或导出周报。使用仓库附带后端并配置个人 Vault 后运行。
metadata:
  version: "1.36.2-share"
---

# Obsidian 工作记录

Obsidian Vault 是工作事实来源。通过统一命令查询和写入，不直接改写程序管理的台账、节点、视图或索引。

## 入口

```bash
python3 "/你的/Skill目录/obsidian-worklog/scripts/record_worklog.py" <命令>
```

仓库包含脱敏后端，不含业务数据。Skill 以符号链接安装时会定位同仓库的后端；若复制 Skill 到别处，需设置 `WORKLOG_ROOT` 为后端绝对路径。先读 [部署与验收](references/setup.md)，具体使用顺序见 [操作流程](references/workflow.md)。遇到覆盖写权限提示时按 [权限弹窗排查](references/setup.md#覆盖写权限弹窗) 处理。命令及参数以已安装后端的帮助输出为准。

## 常见请求

| 请求 | 处理方式 |
|---|---|
| 查看工作、恢复项目背景 | 先 `brief`（近期项目、等待/阻塞任务、收件箱数量），再用 `context --project` 查看指定项目（默认精简，`--full` 看全文）；必要时查询 `tasks`、`search`、`graph` |
| 记录进展或修订 | 先核对既有项目和事实，用 `record-intent-json` 写入（不确定时先 `--dry-run`，字段见 `schema --command record-intent-json`）；写后复查。误写用 `undo` 排除 |
| 先记下、尚未确认的内容 | `capture` 进收件箱，不进项目和周报；确认后 `triage` 转为节点、方法候选、个人笔记或丢弃 |
| 复用经验、阶段复盘 | 检索相关问题与方法，核对适用条件，执行后验证；阶段收尾可用只读 `retro` 汇总证据并生成方法候选草稿 |
| 生成周报 | 指定日期 `preview --raw` 与 `preview --draft-review`（来源已关联的整理稿草稿，看 `draft_notes` 与 `export_ready`），逐项核对来源、去重和统计口径，再导出 |
| 看板 | `dashboard` 导出 JSON 与静态 HTML 到 Vault 之外 |
| 目录、状态和一致性维护 | 先审计、生成具体计划；涉及历史排除、修复、归并、迁移或恢复时取得明确批准 |

## 判断与授权

- 只有已确认、获授权的业务事实进入正式记录。草稿、推测、普通收藏与私人内容不混入业务项目。
- 用户明确要求记录其已陈述事实时，按该范围完成录入与验证，无需重复确认。
- `record-intent-json` 写前会预检（疑似重复、占位、需求池待修、内部分类调整、个人/机制内容、未来时间、等待改写成催办、项目名多义）。返回 `needs_confirmation`（退出码 3）时未写入：向用户说明 `findings`，用户确认后在 JSON 中加 `acknowledged_flags` 重新提交，不自行放行。
- 对历史排除、修复、归并、目录维护与恢复、带遗留事项结项、正式方法确认保留批准点；提供具体对象、内容及影响范围。
- 保留发生日期与录入日期。更正走修订链，不删除原记录；不能因为项目结项就推断所有任务已完成。
- 查询结果可能更新缓存；`ok: true` 和结构审计通过均不代表自然语言事实准确。写后核对项目、任务、视图和本次事实。
