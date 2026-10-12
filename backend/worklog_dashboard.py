"""Read-only dashboard export: one JSON snapshot plus a self-contained static HTML page.

The files are written outside the vault (default ``runtime/dashboard``) so other
pages or tools can read the snapshot without touching Obsidian.
"""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from worklog_base import WorklogError, _atomic_write
from worklog_inbox import list_inbox, pending_age_days


OPEN_TASK_STATUSES = {"待开始", "进行中", "等待中", "阻塞", "待确认"}


def build_snapshot(store: Any, *, today: date | None = None, recent_days: int = 7) -> dict[str, Any]:
    today = today or date.today()
    projects = store.list_projects()
    visible = {item["project_id"]: item for item in projects if item.get("display_in_views", True)}
    tasks = [task for task in store.list_tasks() if task.get("project_id") in visible]
    nodes = [node for node in store.list_nodes() if node.get("project_id") in visible]
    week_start = today - timedelta(days=today.weekday())
    cutoff = (today - timedelta(days=recent_days)).isoformat()

    def day(value: Any) -> str:
        return str(value or "")[:10]

    week_nodes = [node for node in nodes if week_start.isoformat() <= day(node.get("occurred_at")) <= today.isoformat()]
    active = [item for item in visible.values() if item.get("project_status") in {"进行中", "待确认"}]
    inbox = list_inbox(store)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "week_start": week_start.isoformat(),
        "counts": {
            "visible_projects": len(visible),
            "active_projects": len(active),
            "open_tasks": sum(1 for task in tasks if task.get("status") in OPEN_TASK_STATUSES),
            "waiting_or_blocked_tasks": sum(1 for task in tasks if task.get("status") in {"等待中", "阻塞"}),
            "nodes_this_week": len(week_nodes),
            "pending_review_nodes": sum(1 for node in nodes if node.get("review_state") == "待确认"),
            "inbox_pending": len(inbox),
        },
        "active_projects": sorted((
            {"name": item["name"], "status": item.get("project_status"), "domain": item.get("work_domain"),
             "category": item.get("work_category"), "last_activity_at": item.get("last_activity_at"),
             "latest_summary": str(item.get("latest_summary") or "")[:160], "next_action": str(item.get("next_action") or "")[:160],
             "open_task_count": item.get("open_task_count", 0), "recent": day(item.get("last_activity_at")) >= cutoff}
            for item in active), key=lambda item: str(item["last_activity_at"] or ""), reverse=True),
        "waiting_tasks": [
            {"project": visible[task["project_id"]]["name"], "title": task.get("title"), "status": task.get("status"),
             "updated_at": task.get("updated_at")}
            for task in sorted(tasks, key=lambda task: str(task.get("updated_at") or ""))
            if task.get("status") in {"等待中", "阻塞", "待确认"}
        ],
        "this_week": [
            {"occurred_at": node.get("occurred_at"), "project": node.get("project_name"), "kind": node.get("kind"),
             "status": node.get("status"), "summary": str(node.get("summary") or "")[:200]}
            for node in sorted(week_nodes, key=lambda node: str(node.get("occurred_at") or ""), reverse=True)
        ],
        "inbox": [
            {"inbox_id": item.get("inbox_id"), "title": Path(item["path"]).stem, "captured_at": item.get("captured_at"),
             "age_days": pending_age_days(item), "project_hint": item.get("project_hint", "")}
            for item in inbox
        ],
    }


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--text:#1f2328;--muted:#656d76;--line:#d8dee4;--accent:#2f6f4f;--warn:#9a6700}
@media (prefers-color-scheme: dark){:root{--bg:#161b22;--card:#1f242c;--text:#e6edf3;--muted:#8d96a0;--line:#30363d;--accent:#5fb88a;--warn:#d29922}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.tile b{display:block;font-size:24px;color:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px}
.tag{display:inline-block;padding:0 6px;border-radius:4px;border:1px solid var(--line);font-size:12px}.warn{color:var(--warn)}
.wrap{overflow-x:auto}
"""

_LABELS = {
    "visible_projects": "可见项目", "active_projects": "进行中", "open_tasks": "未完成任务",
    "waiting_or_blocked_tasks": "等待/阻塞", "nodes_this_week": "本周进展", "pending_review_nodes": "待确认记录",
    "inbox_pending": "收件箱待处理",
}


def render_html(snapshot: dict[str, Any]) -> str:
    e = lambda value: html.escape(str(value if value is not None else ""))  # noqa: E731

    def table(headers: list[str], rows: list[list[str]], empty: str) -> str:
        if not rows:
            return f'<p class="muted">{e(empty)}</p>'
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    tiles = "".join(f'<div class="tile"><b>{e(value)}</b>{e(_LABELS.get(key, key))}</div>' for key, value in snapshot["counts"].items())
    projects = table(["项目", "状态", "分类", "最近更新", "最新进展", "下一步"], [
        [e(p["name"]) + (' <span class="tag">近期</span>' if p["recent"] else ""), e(p["status"]),
         e(f'{p["domain"] or ""}/{p["category"] or ""}'), e(str(p["last_activity_at"] or "")[:16].replace("T", " ")),
         e(p["latest_summary"]), e(p["next_action"])]
        for p in snapshot["active_projects"]], "没有进行中项目")
    waiting = table(["项目", "任务", "状态", "更新"], [
        [e(t["project"]), e(t["title"]), f'<span class="warn">{e(t["status"])}</span>', e(str(t["updated_at"] or "")[:10])]
        for t in snapshot["waiting_tasks"]], "没有等待或阻塞的任务")
    week = table(["时间", "项目", "类型", "进展"], [
        [e(str(n["occurred_at"] or "")[5:16].replace("T", " ")), e(n["project"]), e(n["kind"]), e(n["summary"])]
        for n in snapshot["this_week"]], "本周还没有进展记录")
    inbox = table(["收件", "天数", "关联提示"], [
        [e(i["title"]), e(i["age_days"]), e(i["project_hint"])] for i in snapshot["inbox"]], "收件箱为空")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>工作全景看板</title><style>{_CSS}</style></head><body><main>
<h1>工作全景看板</h1><div class="muted">生成于 {e(snapshot["generated_at"].replace("T", " "))} · 本周自 {e(snapshot["week_start"])}</div>
<div class="tiles">{tiles}</div>
<h2>进行中项目</h2>{projects}<h2>等待与阻塞</h2>{waiting}<h2>本周进展</h2>{week}<h2>收件箱</h2>{inbox}
</main></body></html>
"""


def export_dashboard(store: Any, output_dir: str | Path, *, today: date | None = None) -> dict[str, Any]:
    output = Path(output_dir)
    vault = store.vault_root.resolve()
    if output.resolve() == vault or vault in output.resolve().parents:
        raise WorklogError("看板导出目录不能位于 Obsidian Vault 内")
    snapshot = build_snapshot(store, today=today)
    _atomic_write(output / "dashboard.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
    _atomic_write(output / "dashboard.html", render_html(snapshot))
    return {"json": str(output / "dashboard.json"), "html": str(output / "dashboard.html"), "counts": snapshot["counts"]}
