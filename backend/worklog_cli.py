from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any

from settings import load_settings
from worklog_core import WorklogError, WorklogStore
from worklog_structure import WORK_DOMAINS


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")


PROJECT_COMPACT_FIELDS = (
    "name", "aliases", "project_status", "work_domain", "work_category",
    "last_activity_at", "latest_summary", "next_action", "open_task_count",
)

CONTEXT_PROJECT_FIELDS = (
    "name", "aliases", "project_status", "work_domain", "work_category",
    "last_activity_at", "latest_summary", "next_action", "open_task_count",
)
CONTEXT_TASK_FIELDS = (
    "task_id", "title", "status", "next_action", "latest_node_id", "required",
)
CONTEXT_NODE_FIELDS = (
    "node_id", "task_id", "occurred_at", "kind", "status", "summary",
    "next_action", "source", "related_path",
)

READ_ONLY_COMMANDS = {
    "projects", "context", "tasks", "search", "list-candidates", "method", "graph", "preview", "audit-integrity",
    "audit-project-materials", "audit-structure", "node", "move-project", "rename-project", "recover-project-maintenance",
}

_TIMING_CONTEXT: dict[str, Any] | None = None
_COMMAND_STORE: WorklogStore | None = None


def emit(payload, code: int = 0, *, compact: bool = False) -> int:
    if _COMMAND_STORE is not None and not _COMMAND_STORE.read_only and isinstance(payload, dict):
        warnings = _COMMAND_STORE.write_warnings()
        if warnings:
            payload = {**payload, "warnings": warnings}
            for warning in warnings:
                print(f"警告 [{warning['code']}] {warning.get('project', '')}：{warning['message']}", file=sys.stderr)
    if _TIMING_CONTEXT is not None and isinstance(payload, dict):
        payload = dict(payload)
        store = _TIMING_CONTEXT["store"]
        store.flush_node_index()
        store.flush_entity_index()
        payload["timing"] = {
            "total_seconds": round(time.perf_counter() - _TIMING_CONTEXT["started"], 4),
            "stages": store.timing_report(),
        }
    options = {"ensure_ascii": False, "default": str}
    if compact:
        options["separators"] = (",", ":")
    else:
        options["indent"] = 2
    print(json.dumps(payload, **options))
    return code


def command_is_read_only(args: argparse.Namespace) -> bool:
    return args.command in READ_ONLY_COMMANDS and not (
        args.command == "audit-project-materials" and args.write_report
    ) and not (
        args.command == "recover-project-maintenance" and args.execute
    )


def compact_json_output(args: argparse.Namespace) -> bool:
    """Keep compact field selection separate from optional pretty serialization."""
    return bool(getattr(args, "compact", False) and not getattr(args, "pretty", False))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为大于 0 的整数")
    return parsed


def _project_activity_date(project: dict[str, Any]) -> date | None:
    for key in ("last_activity_at", "updated_at", "created_at"):
        raw = str(project.get(key) or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def _project_activity_value(project: dict[str, Any]) -> str:
    for key in ("last_activity_at", "updated_at", "created_at"):
        raw = str(project.get(key) or "").strip()
        if raw:
            return raw
    return ""


def select_projects(
    projects: list[dict[str, Any]], *, statuses: list[str] | None = None,
    since_days: int | None = None, visible_only: bool = False,
    sort_by: str = "", limit: int | None = None, compact: bool = False,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter project output while retaining counts that expose intentional truncation."""
    statuses = statuses or []
    selected = list(projects)
    if visible_only:
        selected = [item for item in selected if item.get("display_in_views", True)]
    if statuses:
        allowed = set(statuses)
        selected = [item for item in selected if item.get("project_status") in allowed]
    if since_days is not None:
        cutoff = (today or date.today()) - timedelta(days=since_days)
        selected = [item for item in selected if (_project_activity_date(item) or date.min) >= cutoff]

    if sort_by == "last_activity":
        selected.sort(
            key=lambda item: (_project_activity_value(item), str(item.get("name") or "")),
            reverse=True,
        )
    elif sort_by == "name":
        selected.sort(key=lambda item: str(item.get("name") or "").casefold())

    matched = len(selected)
    if limit is not None:
        selected = selected[:limit]
    if compact:
        selected = [{key: item.get(key) for key in PROJECT_COMPACT_FIELDS} for item in selected]

    meta = {
        "total": len(projects),
        "matched": matched,
        "returned": len(selected),
        "truncated": len(selected) < matched,
        "filters": {
            "status": statuses,
            "since_days": since_days,
            "visible_only": visible_only,
        },
        "sort_by": sort_by or "source",
        "compact": compact,
    }
    return selected, meta


def compact_project_context(context: dict[str, Any], summary_chars: int = 500) -> dict[str, Any]:
    """Return enough stable identifiers for AI continuation without large page bodies."""
    def select(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        result = {key: item.get(key) for key in fields}
        if "summary" in result and len(str(result["summary"] or "")) > summary_chars:
            result["summary"] = str(result["summary"])[:summary_chars].rstrip() + "…"
        return result

    open_tasks = context.get("open_tasks", [])
    completed_tasks = context.get("completed_tasks", [])
    recent_nodes = context.get("recent_nodes", [])
    issues = context.get("issues", [])
    methods = context.get("available_methods", [])
    return {
        "project": select(context.get("project", {}), CONTEXT_PROJECT_FIELDS),
        "open_tasks": [select(item, CONTEXT_TASK_FIELDS) for item in open_tasks],
        "recent_nodes": [select(item, CONTEXT_NODE_FIELDS) for item in recent_nodes],
        "work_lines": context.get("work_lines", []),
        "review_count": context.get("review_count", 0),
        "meta": {
            "completed_task_count": len(completed_tasks),
            "issue_count": len(issues),
            "available_method_count": len(methods),
            "overview_omitted": bool(context.get("overview")),
            "compact": True,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Obsidian 周报系统统一记录命令")
    sub = parser.add_subparsers(dest="command", required=True)
    projects = sub.add_parser("projects", help="列出项目台账")
    projects.add_argument("--status", action="append", default=[], help="按项目状态筛选，可重复")
    projects.add_argument("--since-days", type=_positive_int, help="只返回最近 N 天有活动的项目")
    projects.add_argument("--visible-only", action="store_true", help="只返回进入工作视图的项目")
    projects.add_argument("--sort-by", choices=("name", "last_activity"), default="")
    projects.add_argument("--limit", type=_positive_int, help="限制返回数量；meta.truncated 会标明是否省略")
    projects.add_argument("--compact", action="store_true", help="只返回日常查阅字段并压缩 JSON")
    projects.add_argument("--pretty", action="store_true", help="多行格式化 JSON；可与 --compact 同时使用")
    context = sub.add_parser("context", help="读取项目总览、任务、最近进展和工作线")
    context.add_argument("--project", required=True)
    context.add_argument("--recent", type=int, default=12)
    context.add_argument("--max-chars", type=int, default=18000)
    context.add_argument("--compact", action="store_true", help="省略页面正文和已完成任务详情")
    context.add_argument("--pretty", action="store_true", help="多行格式化 JSON；可与 --compact 同时使用")
    tasks = sub.add_parser("tasks", help="查询任务台账")
    tasks.add_argument("--project", default="")
    tasks.add_argument("--status", action="append", default=[])
    search = sub.add_parser("search", help="搜索项目、任务、进展和成果")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--business-only", action="store_true", help="只返回项目、任务、进展和问题，排除普通页面与方法资产")
    graph = sub.add_parser("graph", help="读取项目主线、支线和节点关系")
    graph.add_argument("--project", required=True)
    node = sub.add_parser("node", help="只读取得指定节点及当前文件哈希，用于受控修订")
    node.add_argument("--node-id", required=True)
    integrity = sub.add_parser("audit-integrity", help="只读核查项目、任务、节点及关系完整性")
    integrity.add_argument("--pretty", action="store_true", help="兼容格式选项；审计默认已输出多行JSON")
    structure = sub.add_parser("audit-structure", help="只读检查项目路径、分类、重复占用及疑似未关联文件")
    structure.add_argument("--pretty", action="store_true", help="兼容格式选项；审计默认已输出多行JSON")
    repair = sub.add_parser("repair-integrity", help="备份并修复重复ID、断链和派生字段")
    repair.add_argument("--views-only", action="store_true", help="仅刷新派生视图，不修改工作节点、任务或业务状态")
    sub.add_parser("revise-overview-json", help="按哈希核对并备份后精确修订项目总览")
    sub.add_parser("save-review-json", help="验证来源后保存周报整理稿，不导出Excel")
    add = sub.add_parser("add-project", help="新增项目")
    add.add_argument("--name", required=True)
    add.add_argument("--major-work", required=True)
    add.add_argument("--key-point", required=True)
    add.add_argument("--status", default="进行中")
    add.add_argument("--overview-path", default="")
    add.add_argument("--work-domain", choices=WORK_DOMAINS, default="", help="显式指定领域，优先于名称推断")
    add.add_argument("--work-category", default="", help="显式指定分类，创建时据此派生默认目录")
    add.add_argument("--work-type", choices=("项目", "持续工作", "单次事项"), default="")
    add.add_argument("--project-mode", choices=("bounded", "ongoing"), default="")
    setup_experience = sub.add_parser("setup-project-experience", help="为项目建立流程、问题、产出和复盘目录")
    setup_experience.add_argument("--project", required=True)
    setup_experience.add_argument("--source-root", default="")
    index_materials = sub.add_parser("index-project-materials", help="生成项目资料、课件与视频的可搜索索引")
    index_materials.add_argument("--project", required=True)
    index_materials.add_argument("--source-root", required=True)
    audit_materials = sub.add_parser("audit-project-materials", help="核查资料索引的漏录、重复和识别错误")
    audit_materials.add_argument("--project", required=True)
    audit_materials.add_argument("--source-root", required=True)
    audit_materials.add_argument("--write-report", action="store_true", help="将核查结果写入项目产出页")
    record = sub.add_parser("record", help="记录一个工作节点")
    record.add_argument("--project", required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--status", default="进行中")
    record.add_argument("--next-action", default="")
    record.add_argument("--kind", default="进展")
    record.add_argument("--source", default="手工记录")
    record.add_argument("--occurred-at")
    record.add_argument("--related-path", default="")
    record.add_argument("--exclude-from-report", action="store_true")
    record.add_argument("--track-type", default="主线")
    record.add_argument("--lane-name", default="")
    record.add_argument("--relation-type", default="顺序")
    record.add_argument("--predecessor", action="append", default=[])
    record.add_argument("--parent-node-id", default="")
    record.add_argument("--keep-open", action="store_true")
    record.add_argument("--task-id", default="")
    record.add_argument("--task-status", default="")
    record.add_argument("--intent-type", default="")
    sub.add_parser("record-json", help="从标准输入读取JSON并记录节点")
    sub.add_parser("record-intent-json", help="从标准输入读取AI判断后的记录意图")
    sub.add_parser("revise-node-json", help="从标准输入修订已有节点并保留历史关系")
    sub.add_parser("exclude-node-json", help="从标准输入排除误归档节点及其完整修订链")
    sub.add_parser("record-issue-json", help="从标准输入读取问题与决策记录")
    sub.add_parser("resolve-issue-json", help="从标准输入更新问题卡的决策、结果或状态")
    sub.add_parser("revise-method-json", help="按哈希受控修订正式方法并保留快照")
    sub.add_parser("refresh-method-index", help="刷新正式方法卡索引受控区")
    candidates = sub.add_parser("list-candidates", help="跨领域列出方法候选")
    candidates.add_argument("--project", default="")
    method = sub.add_parser("method", help="读取正式方法及修订用哈希")
    method.add_argument("--method-id", required=True)
    sub.add_parser("propose-method-json", help="从标准输入写入方法论候选")
    sub.add_parser("record-workflow-json", help="从标准输入写入非业务工作机制资产")
    confirm_method = sub.add_parser("confirm-method", help="确认方法论候选并进入正式方法论库")
    confirm_method.add_argument("--candidate-id", required=True)
    confirm_method.add_argument("--expected-hash", default="")
    close_project = sub.add_parser("close-project", help="生成项目复盘并结项")
    close_project.add_argument("--project", required=True)
    close_project.add_argument("--summary", default="")
    close_project.add_argument("--allow-open", action="store_true")
    sub.add_parser("normalize-existing-projects", help="为已有项目总览补齐统一项目结构")
    sub.add_parser("humanize-storage", help="清理历史合并台账和审计节点中的内部编号文件名")
    update_project = sub.add_parser("update-project", help="更新项目状态、分类或别名")
    update_project.add_argument("--project", required=True)
    update_project.add_argument("--name")
    update_project.add_argument("--status")
    update_project.add_argument("--major-work")
    update_project.add_argument("--key-point")
    update_project.add_argument("--work-domain")
    update_project.add_argument("--work-category")
    update_project.add_argument("--work-type")
    update_project.add_argument("--project-mode")
    update_project.add_argument("--overview-path")
    update_project.add_argument("--display-in-views", choices=("true", "false"))
    update_project.add_argument("--view-tag", action="append", default=[], help="全量替换标签集合；重复传入所有需保留的标签")
    update_project.add_argument("--add-alias", action="append", default=[])
    move_project = sub.add_parser("move-project", help="只读预览单项目目录搬移及引用变更，不直接执行")
    move_project.add_argument("--project", required=True)
    move_project.add_argument("--to-category", required=True)
    move_project.add_argument("--to-domain", choices=WORK_DOMAINS, default="")
    move_project.add_argument("--to-directory", default="", help="可选：所选领域内独立项目目录的Vault相对路径")
    move_project.add_argument("--dry-run", action="store_true", help="兼容选项：本命令始终只读预览")
    rename_project = sub.add_parser("rename-project", help="只读预览项目改名、台账/任务目录整理及旧关系图备份")
    rename_project.add_argument("--project", required=True)
    rename_project.add_argument("--name", required=True)
    rename_project.add_argument("--dry-run", action="store_true", help="兼容选项：本命令始终只读预览")
    apply_maintenance = sub.add_parser("apply-project-maintenance", help="按已审阅计划及完整哈希执行维护，失败自动恢复")
    apply_maintenance.add_argument("--plan-file", required=True)
    apply_maintenance.add_argument("--expected-plan-hash", required=True)
    recover = sub.add_parser("recover-project-maintenance", help="检查维护事务恢复条件；仅--execute执行恢复")
    recover.add_argument("--plan-hash", required=True)
    recover.add_argument("--execute", action="store_true")
    merge_projects = sub.add_parser("merge-projects", help="将重复项目合并到一个正式项目")
    merge_projects.add_argument("--canonical", required=True)
    merge_projects.add_argument("--duplicate", action="append", required=True)
    merge_tasks = sub.add_parser("merge-tasks", help="将同项目内的重复任务合并到一个正式任务")
    merge_tasks.add_argument("--canonical-task", required=True)
    merge_tasks.add_argument("--duplicate-task", action="append", required=True)
    move_task = sub.add_parser("move-task", help="将误分类任务及其工作节点迁移到正确项目")
    move_task.add_argument("--task-id", required=True)
    move_task.add_argument("--target-project", required=True)
    complete_project = sub.add_parser("complete-project", help="旧批量结项入口：会关闭全部未完成任务；通常请用 close-project")
    complete_project.add_argument("--project", required=True)
    complete_project.add_argument("--confirm-all-open-tasks", action="store_true", help="仅在用户明确确认全部遗留任务完成后使用")
    close_superseded = sub.add_parser("close-superseded-tasks", help="将已有后续节点的历史旧任务批量标记为已完成")
    close_superseded.add_argument("--dry-run", action="store_true", help="仅列出将调整的任务，不写入")
    close_superseded_nodes = sub.add_parser("close-superseded-nodes", help="将同一工作线中已有后续节点的历史节点批量标记为已完成")
    close_superseded_nodes.add_argument("--dry-run", action="store_true", help="仅列出将调整的节点，不写入")
    close_superseded_nodes.add_argument("--project", default="")
    close_superseded_nodes.add_argument("--track-type", choices=("主线", "支线", "子任务"), default="")
    add_task = sub.add_parser("add-task", help="新增任务")
    add_task.add_argument("--project", required=True)
    add_task.add_argument("--title", required=True)
    add_task.add_argument("--status", default="待开始")
    add_task.add_argument("--description", default="")
    add_task.add_argument("--optional", action="store_true")
    update_task = sub.add_parser("task-update", help="更新任务状态或名称")
    update_task.add_argument("--task-id", required=True)
    update_task.add_argument("--status")
    update_task.add_argument("--title")
    update_task.add_argument("--related-path")
    update_task.add_argument("--description")
    update_task.add_argument("--track-type", choices=("主线", "支线", "子任务"))
    update_task.add_argument("--lane-name")
    sub.add_parser("setup-panorama", help="迁移并刷新个人工作全景")
    preview = sub.add_parser("preview", help="生成周报预览JSON")
    preview.add_argument("--start")
    preview.add_argument("--end")
    preview.add_argument("--raw", action="store_true", help="返回完整候选事项及来源，不复用已核验整理稿")
    export = sub.add_parser("export", help="生成并验证周报Excel")
    export.add_argument("--start")
    export.add_argument("--end")
    export.add_argument("--output-dir", default="")
    export.add_argument("--review-file", default="", help="带来源关联及预览指纹的内容整理JSON")
    sub.add_parser("import-history", help="导入历史周报和Obsidian里程碑")
    for command_parser in sub.choices.values():
        command_parser.add_argument(
            "--timing", action="store_true",
            help="在JSON结果中增加锁等待、节点加载和派生刷新耗时；默认不输出",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    global _TIMING_CONTEXT, _COMMAND_STORE
    command_started = time.perf_counter()
    settings = load_settings()
    args = build_parser().parse_args(argv)
    if args.command == "complete-project" and not args.confirm_all_open_tasks:
        # Reject before store initialization or schema migration can write files.
        print(json.dumps({"ok": False, "error": "complete-project 会批量关闭全部未完成任务。一般结项请使用 close-project；仅当用户明确确认全部遗留任务完成时，才可加 --confirm-all-open-tasks。"}, ensure_ascii=False))
        return 2
    if args.command in {"move-project", "rename-project", "apply-project-maintenance", "recover-project-maintenance"}:
        # Maintenance owns its lock, snapshot and transaction. Never migrate or
        # initialize business files before validating the reviewed plan.
        from worklog_maintenance import plan_maintenance, apply_maintenance, recover_maintenance
        _TIMING_CONTEXT = None
        _COMMAND_STORE = None
        maintenance_store = WorklogStore(settings["vault_root"], read_only=True)
        try:
            if args.command == "move-project":
                result = plan_maintenance(maintenance_store, {"operation": "move", "project": args.project,
                    "to_domain": args.to_domain, "to_category": args.to_category, "to_directory": args.to_directory})
                return emit({"ok": True, "plan": result})
            if args.command == "rename-project":
                return emit({"ok": True, "plan": plan_maintenance(maintenance_store,
                    {"operation": "rename", "project": args.project, "name": args.name})})
            if args.command == "apply-project-maintenance":
                payload = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
                return emit({"ok": True, "result": apply_maintenance(maintenance_store,
                    payload.get("plan", payload), args.expected_plan_hash)})
            return emit({"ok": True, "result": recover_maintenance(maintenance_store, args.plan_hash, execute=args.execute)})
        except (WorklogError, OSError, ValueError) as exc:
            return emit({"ok": False, "error": str(exc)}, 2)
    read_only = command_is_read_only(args)
    store = WorklogStore(
        settings["vault_root"], read_only=read_only,
        node_index_path=settings.get("node_index_path"),
        entity_index_path=settings.get("entity_index_path"),
    )
    store.enable_timing(args.timing)
    _COMMAND_STORE = store
    _TIMING_CONTEXT = {"started": command_started, "store": store} if args.timing else None
    process_lock = None
    snapshot_scope = store.node_snapshot_scope()
    try:
        snapshot_scope.__enter__()
        if not read_only:
            process_lock = store.mutation_lock()
            process_lock.__enter__()
        with store.timed_stage("migration_check"):
            migration = {"migrated": False, "reason": "read-only"} if read_only else store.migrate_schema_v6()
        if args.command == "projects":
            projects, meta = select_projects(
                store.list_projects(), statuses=args.status, since_days=args.since_days,
                visible_only=args.visible_only, sort_by=args.sort_by, limit=args.limit,
                compact=args.compact,
            )
            return emit({"ok": True, "meta": meta, "projects": projects}, compact=compact_json_output(args))
        if args.command == "context":
            with store.timed_stage("command_context"):
                context_result = store.project_context(args.project, args.recent, args.max_chars)
            if args.compact:
                context_result = compact_project_context(context_result)
            return emit({"ok": True, "context": context_result}, compact=compact_json_output(args))
        if args.command == "tasks":
            return emit({"ok": True, "tasks": store.tasks_with_current_progress(store.list_tasks(args.project, args.status))})
        if args.command == "search":
            search_limit = max(args.limit * 5, 100) if args.business_only else args.limit
            results = store.search(args.query, search_limit)
            if args.business_only:
                results = [item for item in results if item.get("kind") in {"project", "task", "node", "issue"}][:args.limit]
            return emit({"ok": True, "results": results})
        if args.command == "graph":
            project = store.get_project(args.project)
            if not project:
                raise WorklogError(f"项目未登记：{args.project}")
            return emit({"ok": True, "graph": store.project_graph(project["project_id"])})
        if args.command == "node":
            node = store.get_node(args.node_id)
            if not node:
                raise WorklogError(f"节点不存在：{args.node_id}")
            return emit({"ok": True, "node": node})
        if args.command == "audit-integrity":
            with store.timed_stage("command_audit_integrity"):
                audit = store.audit_integrity()
            return emit({"ok": audit["ok"], "audit": audit}, 0 if audit["ok"] else 2)
        if args.command == "audit-structure":
            with store.timed_stage("command_audit_structure"):
                audit = store.audit_structure()
            return emit({"ok": audit["ok"], "audit": audit}, 0 if audit["ok"] else 2)
        if args.command == "repair-integrity":
            result = store.repair_integrity(views_only=args.views_only)
            return emit({"ok": result["audit"]["ok"], "result": result}, 0 if result["audit"]["ok"] else 2)
        if args.command == "revise-overview-json":
            payload = json.load(sys.stdin)
            result = store.revise_overview(payload.get("project", ""), payload.get("expected_hash", ""), payload.get("replacements", []), payload.get("reason", ""))
            return emit({"ok": True, "result": result})
        if args.command == "save-review-json":
            from reporting import build_report_preview
            from report_content import apply_review
            from worklog_core import _atomic_write
            payload = json.load(sys.stdin)
            period = payload.get("range", {})
            report = build_report_preview(store, period.get("start"), period.get("end"), use_review=False)
            reviewed = apply_review(report, payload)
            destination = store.drafts_dir / f"{report['range']['start']}_{report['range']['end']}.content-review.json"
            if destination.exists():
                import shutil
                backup = store.backups_dir / f"review-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            _atomic_write(destination, json.dumps(payload, ensure_ascii=False, indent=2))
            return emit({"ok": True, "path": str(destination), "report": reviewed})
        if args.command == "add-project":
            project = store.create_project(
                name=args.name, major_work=args.major_work, key_point=args.key_point,
                status=args.status, overview_path=args.overview_path, create_overview=True,
                work_domain=args.work_domain, work_category=args.work_category,
                work_type=args.work_type, project_mode=args.project_mode,
            )
            return emit({"ok": True, "project": project})
        if args.command == "setup-project-experience":
            return emit({"ok": True, "experience": store.initialize_project_experience(args.project, args.source_root)})
        if args.command == "index-project-materials":
            return emit({"ok": True, "index": store.build_material_index(args.project, args.source_root)})
        if args.command == "audit-project-materials":
            audit = store.audit_material_index(args.project, args.source_root, write_report=args.write_report)
            return emit({"ok": audit["ok"], "audit": audit}, 0 if audit["ok"] else 2)
        if args.command == "record-json":
            payload = json.load(sys.stdin)
            node = store.record_node(
                project=payload.get("project_id") or payload.get("project", ""), summary=payload.get("summary", ""),
                status=payload.get("status", "进行中"), next_action=payload.get("next_action", ""), kind=payload.get("kind", "进展"),
                source=payload.get("source", "手工记录"), occurred_at=payload.get("occurred_at"), related_path=payload.get("related_path", ""),
                include_in_report=payload.get("include_in_report", True),
                idempotency_key=payload.get("idempotency_key", ""),
                lane_id=payload.get("lane_id", ""), lane_name=payload.get("lane_name", ""), track_type=payload.get("track_type", "主线"),
                relation_type=payload.get("relation_type", "顺序"), predecessor_ids=payload.get("predecessor_ids") or [],
                parent_node_id=payload.get("parent_node_id", ""), start_time=payload.get("start_time"), end_time=payload.get("end_time"),
                keep_open=payload.get("keep_open", False),
                task_id=payload.get("task_id", ""), task_status=payload.get("task_status", ""), intent_type=payload.get("intent_type", ""),
            )
            return emit({"ok": True, "node": node})
        if args.command == "record-intent-json":
            with store.timed_stage("command_record_intent"):
                result = store.record_intent(json.load(sys.stdin))
            return emit({"ok": True, "result": result})
        if args.command == "revise-node-json":
            payload = json.load(sys.stdin)
            node_id = str(payload.get("node_id") or "").strip()
            expected_hash = str(payload.get("expected_hash") or "").strip()
            changes = payload.get("changes") or {
                key: value for key, value in payload.items()
                if key not in {"node_id", "expected_hash"}
            }
            if not node_id or not expected_hash:
                raise WorklogError("revise-node-json需要node_id和expected_hash")
            return emit({"ok": True, "node": store.revise_node(node_id, changes, expected_hash)})
        if args.command == "exclude-node-json":
            payload = json.load(sys.stdin)
            node_id = str(payload.get("node_id") or "").strip()
            expected_hash = str(payload.get("expected_hash") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            if not node_id or not expected_hash or not reason:
                raise WorklogError("exclude-node-json需要node_id、expected_hash和reason")
            with store.timed_stage("command_exclude_node"):
                result = store.exclude_node(node_id, expected_hash, reason)
            return emit({"ok": True, "result": result})
        if args.command == "record-issue-json":
            return emit({"ok": True, "issue": store.record_issue(json.load(sys.stdin))})
        if args.command == "resolve-issue-json":
            payload = json.load(sys.stdin)
            return emit({"ok": True, "issue": store.resolve_issue(str(payload.get("issue_id") or ""), payload)})
        if args.command == "list-candidates":
            return emit({"ok": True, "candidates": store.list_candidates(args.project)})
        if args.command == "method":
            matches = [m for m in store.list_methods(status="") if m.get("method_id") == args.method_id]
            if len(matches) != 1:
                raise WorklogError("正式方法不存在或ID不唯一")
            return emit({"ok": True, "method": matches[0]})
        if args.command == "refresh-method-index":
            return emit({"ok": True, "index": store.refresh_method_index()})
        if args.command == "revise-method-json":
            return emit({"ok": True, "method": store.revise_method(json.load(sys.stdin))})
        if args.command == "propose-method-json":
            return emit({"ok": True, "candidate": store.propose_method(json.load(sys.stdin))})
        if args.command == "record-workflow-json":
            from workflow_assets import record_workflow_asset
            return emit({"ok": True, "workflow": record_workflow_asset(store, json.load(sys.stdin))})
        if args.command == "confirm-method":
            return emit({"ok": True, "method": store.confirm_method(args.candidate_id, args.expected_hash)})
        if args.command == "close-project":
            return emit({"ok": True, "result": store.close_project(args.project, allow_open=args.allow_open, summary=args.summary)})
        if args.command == "normalize-existing-projects":
            return emit({"ok": True, "result": store.normalize_existing_projects()})
        if args.command == "humanize-storage":
            return emit({"ok": True, "result": store.humanize_legacy_storage()})
        if args.command == "update-project":
            project = store.get_project(args.project)
            if not project:
                raise WorklogError(f"项目未登记：{args.project}")
            aliases = sorted({*(project.get("aliases") or []), *args.add_alias})
            changes = {
                key: value for key, value in {
                    "name": args.name, "project_status": args.status, "major_work": args.major_work, "key_point": args.key_point,
                    "work_domain": args.work_domain, "work_category": args.work_category,
                    "work_type": args.work_type, "project_mode": args.project_mode,
                    "overview_path": args.overview_path,
                }.items() if value is not None
            }
            if args.display_in_views is not None:
                changes["display_in_views"] = args.display_in_views == "true"
            if args.add_alias:
                changes["aliases"] = aliases
            if args.view_tag:
                changes["view_tags"] = sorted(set(args.view_tag))
            return emit({"ok": True, "project": store.update_project(project["project_id"], changes)})
        if args.command == "merge-projects":
            return emit({"ok": True, "project": store.merge_projects(args.canonical, args.duplicate)})
        if args.command == "merge-tasks":
            return emit({"ok": True, "task": store.merge_tasks(args.canonical_task, args.duplicate_task)})
        if args.command == "move-task":
            return emit({"ok": True, "task": store.move_task(args.task_id, args.target_project)})
        if args.command == "complete-project":
            return emit({"ok": True, "result": store.complete_project(args.project)})
        if args.command == "close-superseded-tasks":
            return emit({"ok": True, "result": store.close_superseded_tasks(dry_run=args.dry_run)})
        if args.command == "close-superseded-nodes":
            return emit({"ok": True, "result": store.close_superseded_nodes(
                dry_run=args.dry_run, project=args.project, track_type=args.track_type)})
        if args.command == "add-task":
            task = store.create_task(
                project=args.project, title=args.title, status=args.status,
                description=args.description, required=not args.optional,
            )
            store.write_work_panorama_pages()
            return emit({"ok": True, "task": task})
        if args.command == "task-update":
            changes = {
                key: value for key, value in {
                    "status": args.status, "title": args.title, "related_path": args.related_path,
                    "description": args.description, "track_type": args.track_type,
                    "lane_name": args.lane_name,
                }.items() if value is not None
            }
            task = store.update_task(args.task_id, changes)
            store.write_work_panorama_pages()
            return emit({"ok": True, "task": task})
        if args.command == "record":
            node = store.record_node(
                project=args.project, summary=args.summary, status=args.status, next_action=args.next_action, kind=args.kind,
                source=args.source, occurred_at=args.occurred_at, related_path=args.related_path, include_in_report=not args.exclude_from_report,
                track_type=args.track_type, lane_name=args.lane_name, relation_type=args.relation_type,
                predecessor_ids=args.predecessor, parent_node_id=args.parent_node_id, keep_open=args.keep_open,
                task_id=args.task_id, task_status=args.task_status, intent_type=args.intent_type,
            )
            return emit({"ok": True, "node": node})
        if args.command == "setup-panorama":
            store.sync_native_views()
            store.write_work_panorama_pages()
            return emit({"ok": True, "migration": migration, "projects": len(store.list_projects()), "tasks": len(store.list_tasks())})
        if args.command == "preview":
            from reporting import build_report_preview
            return emit({"ok": True, "report": build_report_preview(store, args.start, args.end, use_review=not args.raw)})
        if args.command == "export":
            from reporting import build_report_preview
            from weekly_export import (
                export_weekly_report,
                prepare_weekly_report_items,
                verify_weekly_report,
            )
            report = build_report_preview(store, args.start, args.end)
            review = json.loads(Path(args.review_file).read_text(encoding="utf-8")) if args.review_file else None
            output = export_weekly_report(
                store,
                template_path=settings.get("template_path"),
                export_dir=args.output_dir or settings["export_dir"],
                start=args.start,
                end=args.end,
                current_items=report["current_items"],
                plan_items=report["plan_items"],
                review=review,
                source_digest=report["source_digest"],
            )
            verified = json.loads(output.with_suffix(".verification.json").read_text(encoding="utf-8"))
            return emit({
                "ok": True,
                "output": str(output),
                "range": report["range"],
                "current_items": len(verified["current_items"]),
                "plan_items": len(verified["plan_items"]),
                "verification": verified["verification"],
            })
        if args.command == "import-history":
            from history_import import import_all
            return emit({"ok": True, "result": import_all(store, settings["weekly_source_dir"])})
    except WorklogError as exc:
        return emit({"ok": False, "error": str(exc)}, 2)
    finally:
        store.flush_node_index()
        store.flush_entity_index()
        if process_lock is not None:
            process_lock.__exit__(None, None, None)
        snapshot_scope.__exit__(None, None, None)
        _TIMING_CONTEXT = None
        _COMMAND_STORE = None
    return emit({"ok": False, "error": "未知命令"}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
