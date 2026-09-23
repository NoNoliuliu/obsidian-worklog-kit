"""Evidence-bound weekly report editing; never infer semantic supersession by task id."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from worklog_core import WorklogError

SECTIONS = ("current_items", "plan_items")
MANAGEMENT_ONLY = re.compile(r"^(?:\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,2})?\s*)?(?:重新分类|归入培训实施|项目分类调整|项目归类调整)")
PRIVATE_TEXT = re.compile(r"(?:/(?:Users|home|private|var|tmp)/|(?<![A-Za-z])[A-Za-z]:[\\/]|file://|\b(?:prj|task|node)-[a-zA-Z0-9-]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")


def leader_text(text: str) -> str:
    """Conservative mechanical cleanup only. Business synthesis belongs in a sourced review."""
    text = strip_management_noise(str(text))
    text = re.sub(r"\[([^\]]+)\]\((?:file:|https?:)[^)]*\)", r"\1", text)
    # Path clauses can contain spaces and parentheses; stop at a sentence/semicolon.
    text = re.sub(r"[，,]?\s*(?:数据文件已存于|资料目录[：:]|资料路径[：:]|海报文件[：:]|通知邮件[：:]|审核计划文件[：:]|原始[^。；]*?(?:保存在|保存于)|位于)[^。；]*", "", text)
    text = re.sub(r"(?:/(?:Users|home|private|var|tmp)/|(?<![A-Za-z])[A-Za-z]:[\\/]|file://)[^。；\n]*", "", text)
    text = re.sub(r"(?:链接为|链接[：:])?\s*https?://[^\s。；）)]+", "", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "", text)
    text = re.sub(r"\b(?:prj|task|node)-[a-zA-Z0-9-]+", "", text)
    text = re.sub(r"用户(?:确认|澄清)[：:]?\s*", "", text)
    text = re.sub(r"（\s*）|\(\s*\)", "", text)
    text = re.sub(r"[；。]\s*[；。]+", "。", text)
    return text.strip(" \n；，")


def strip_management_noise(text: str) -> str:
    return ''.join(part for part in re.split(r'(?<=[。；\n])', text)
                   if not MANAGEMENT_ONLY.match(part.strip())).strip()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def attach_evidence(report: dict, nodes: list[dict], tasks: list[dict], projects: list[dict]) -> dict:
    """Include complete dated sources for internal synthesis, never in the Excel cells."""
    visible = {i["project_id"] for section in SECTIONS for i in report[section]}
    fields = ("node_id", "project_id", "task_id", "lane_id", "occurred_at", "created_at", "kind", "summary", "next_action", "supersedes", "include_in_report", "status", "predecessor_ids")
    sources = [{k: n.get(k) for k in fields} for n in nodes if n.get("project_id") in visible]
    report["sources"] = sources
    report["source_digest"] = fingerprint({"range": report["range"], "sources": sources,
        "tasks": [{k: t.get(k) for k in ("task_id", "project_id", "status", "status_history", "next_action", "title")} for t in tasks if t.get("project_id") in visible],
        "projects": [{k: p.get(k) for k in ("project_id", "name", "display_in_views", "major_work", "work_domain", "work_category")} for p in projects],
        "plans": report["plan_items"], "rules": "weekly-content-v2"})
    # Free text needs semantic review even when a project has only one record.
    report["review_required"] = bool(report["current_items"] or report["plan_items"])
    return report


def apply_review(report: dict, review: dict) -> dict:
    """Validate provenance/coverage, not semantic truth. The author must check every claim."""
    if review.get("source_digest") != report.get("source_digest") or review.get("range") != report.get("range"):
        raise WorklogError("周报底稿已变化或日期不匹配，请重新预览并核验整理稿")
    result = copy.deepcopy(report)
    for section in SECTIONS:
        original = {i["project_id"]: i for i in report[section]}
        supplied = review.get(section)
        if not isinstance(supplied, list):
            raise WorklogError("整理稿缺少本周进展或下周计划")
        if len({i.get('project_id') for i in supplied}) != len(supplied):
            raise WorklogError("整理稿项目重复")
        omitted = review.get("omitted_" + section, {})
        if not isinstance(omitted, dict) or any(not str(reason).strip() for reason in omitted.values()):
            raise WorklogError("排除事项必须记录理由")
        if set(original) != {i.get("project_id") for i in supplied} | set(omitted) or set(omitted) & {i.get("project_id") for i in supplied}:
            raise WorklogError("整理稿遗漏或增加项目，排除项目须说明理由")
        rows = []
        for item in supplied:
            base = original[item["project_id"]]
            points = item.get("points", [])
            if not points or any(not isinstance(p, str) or not p.strip() or PRIVATE_TEXT.search(p) for p in points):
                raise WorklogError("整理稿正文为空或仍含路径、内部编号、邮箱")
            if section == "current_items":
                groups = item.get("point_sources", [])
                if len(groups) != len(points) or any(not g for g in groups):
                    raise WorklogError("每条本周进展必须关联来源节点")
                covered = {n for g in groups for n in g}
                excluded = item.get("omitted_sources", {})
                if any(not str(r).strip() for r in excluded.values()):
                    raise WorklogError("省略节点必须说明理由")
                if covered & set(excluded) or covered | set(excluded) != set(base.get("source_node_ids", [])):
                    raise WorklogError("整理稿未覆盖当周全部来源，或引用了其他项目/周次的节点")
            elif points != base["points"]:
                groups = item.get("plan_sources", [])
                excluded = item.get("omitted_actions", {})
                if len(groups) != len(points) or any(not g for g in groups):
                    raise WorklogError("改写计划须关联原计划来源")
                covered = {n for g in groups for n in g}
                if any(not str(r).strip() for r in excluded.values()) or covered & set(excluded) or covered | set(excluded) != set(base.get("source_action_ids", [])):
                    raise WorklogError("整理稿遗漏并行计划，或未说明省略理由")
            rows.append({**base, "points": [p.strip() for p in points]})
        result[section] = rows
    result["review_required"] = False
    result["review"] = review
    return result
