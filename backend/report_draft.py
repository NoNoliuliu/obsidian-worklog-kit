"""Pre-filled weekly review draft: sources wired, wording left to the reviewer.

``build_review_draft`` turns a raw preview into a review JSON that already passes
``apply_review``'s provenance checks whenever the cleaned text allows: every
current-week node becomes one point (identical cleaned text is merged and keeps
all of its sources), plan items keep their points. ``draft_notes`` lists what the
reviewer still has to decide: points in the same task that may be one continuing
item, text that still contains paths or internal IDs, very long points. The draft
is never saved by this module and never replaces the business review.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from typing import Any

from report_content import PRIVATE_TEXT, apply_review, leader_text
from worklog_base import WorklogError


LONG_POINT = 200


def build_review_draft(report: dict[str, Any], store: Any = None) -> dict[str, Any]:
    """``store`` enables the export checks (Excel row height, residual private text) behind ``export_ready``."""
    sources = {item["node_id"]: item for item in report.get("sources", [])}
    notes: list[dict[str, Any]] = []
    current_items = []
    for item in report.get("current_items", []):
        points: OrderedDict[str, list[str]] = OrderedDict()
        tasks: dict[str, list[str]] = {}
        ordered = sorted(item.get("source_node_ids", []), key=lambda node_id: str(sources.get(node_id, {}).get("occurred_at", "")))
        for node_id in ordered:
            node = sources.get(node_id, {})
            text = leader_text(str(node.get("summary") or "")) or str(node.get("summary") or "").strip()
            points.setdefault(text, []).append(node_id)
            if node.get("task_id"):
                tasks.setdefault(str(node["task_id"]), []).append(text)
        point_list = list(points)
        for task_id, texts in tasks.items():
            distinct = list(dict.fromkeys(texts))
            if len(distinct) > 1:
                notes.append({"project": item.get("project_name"), "type": "same_task",
                              "message": f"同一任务有 {len(distinct)} 条进展：同一期次的连续推进可合并为最终进展，不同期次/交付物分别保留",
                              "points": [point_list.index(text) for text in distinct]})
        for index, text in enumerate(point_list):
            if PRIVATE_TEXT.search(text):
                notes.append({"project": item.get("project_name"), "type": "private_text", "point": index,
                              "message": "仍含路径、内部编号或邮箱，需改写"})
            if len(text) > LONG_POINT:
                notes.append({"project": item.get("project_name"), "type": "long_point", "point": index,
                              "message": f"超过 {LONG_POINT} 字，建议压缩为领导阅读口径"})
        current_items.append({
            "project_id": item["project_id"],
            "project_name": item.get("project_name"),
            "points": point_list,
            "point_sources": list(points.values()),
            "omitted_sources": {},
        })
    plan_items = []
    for item in report.get("plan_items", []):
        for index, text in enumerate(item.get("points", [])):
            if PRIVATE_TEXT.search(text):
                notes.append({"project": item.get("project_name"), "type": "private_text", "section": "plan",
                              "point": index, "message": "下周计划仍含路径、内部编号或邮箱，改写时须同时提供 plan_sources"})
        plan_items.append({"project_id": item["project_id"], "project_name": item.get("project_name"),
                           "points": list(item.get("points", []))})
    draft = {
        "range": copy.deepcopy(report["range"]),
        "source_digest": report["source_digest"],
        "current_items": current_items,
        "plan_items": plan_items,
        "omitted_current_items": {},
        "omitted_plan_items": {},
        "draft_notes": notes,
    }
    try:
        reviewed = apply_review(report, draft)
        draft["draft_valid"] = True
    except WorklogError as exc:
        reviewed = None
        draft["draft_valid"] = False
        draft["draft_error"] = str(exc)
    if store is not None:
        blockers = export_blockers(store, reviewed) if reviewed is not None else []
        notes.extend(blockers)
        draft["export_ready"] = reviewed is not None and not blockers
    return draft


def export_blockers(store: Any, reviewed: dict[str, Any]) -> list[dict[str, Any]]:
    """Same text and row-height checks export_weekly_report applies before writing Excel."""
    from weekly_export import EXCEL_MAX_ROW_HEIGHT, content_height, content_text, prepare_weekly_report_items

    blockers = []
    for section, label in (("current_items", "本周进展"), ("plan_items", "下周计划")):
        for item in prepare_weekly_report_items(store, reviewed[section]):
            text = content_text(item)
            if PRIVATE_TEXT.search(text):
                blockers.append({"project": item.get("project_name"), "type": "export_private_text", "section": label,
                                 "message": "导出前须删除路径、内部编号或邮箱"})
            height = content_height(text)
            if height > EXCEL_MAX_ROW_HEIGHT:
                blockers.append({"project": item.get("project_name"), "type": "export_too_tall", "section": label,
                                 "message": f"单元格约需行高 {height:.0f}，超过 Excel 上限 {EXCEL_MAX_ROW_HEIGHT}；合并连续进展或压缩措辞",
                                 "chars": len(text)})
    return blockers
