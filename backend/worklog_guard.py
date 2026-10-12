"""Pre-write checks and dry-run planning for record-intent-json.

Rules that used to live only in the Skill prompt are checked here before any
file is written. A finding blocks the write until the caller resends the same
payload with the finding's code in ``acknowledged_flags`` (after the user has
confirmed). Checks never modify the vault.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta
from typing import Any, Iterable

from worklog_core import (
    VALID_INTENT_TYPES, UnknownProjectError, WorklogError, _search_normalize, normalize_source,
)


DUPLICATE_RATIO = 0.93
DUPLICATE_WINDOW_DAYS = 45
FUTURE_TOLERANCE = timedelta(hours=12)
OPEN_TASK_STATUSES = {"待开始", "进行中", "等待中", "阻塞", "待确认"}

PLACEHOLDER_PATTERN = re.compile(r"^(test|testing|测试|todo|tbd|xxx+|占位|demo|示例)\W*$", re.I)
BACKLOG_PATTERN = re.compile(r"先记录[、，,]?\s*(?:之后|以后|后续|再)修复|之后再修|待统一(?:处理|修复)|需求池\s*bug|作为需求池", re.I)
INTERNAL_ADMIN_PATTERN = re.compile(
    r"归类调整|分类调整|重新分类|view_tags|overview\s*路径|内部结构调整|台账结构|仅为内部|不计入业务进展"
)
CHASE_PATTERN = re.compile(r"催办|催促|督促|追问|催一下")
PERSONAL_PATTERN = re.compile(
    r"个人探索|个人学习|私人|个人笔记|学习笔记|发布\s*Skill|Skill\s*(?:发布|同步|版本)|工作机制|归档规则|AI\s*协同规则",
    re.I,
)

FLAG_MESSAGES = {
    "possible_duplicate": "同项目已有几乎相同的工作记录，可能是重试或重复导入",
    "placeholder_summary": "摘要像测试或占位内容",
    "backlog_item": "像需求池/待统一修复的问题，通常不作为工作记录",
    "internal_admin": "像分类、结构或视图的内部调整，通常不作为业务进展",
    "future_occurred_at": "发生时间晚于当前时间",
    "waiting_as_chase": "等待对方反馈时下一步写成了催办/督促",
    "personal_or_mechanism": "像个人内容、Skill 资产或工作机制，不属于业务工作记录",
    "ambiguous_project": "项目名能匹配多个项目",
}


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _finding(code: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": FLAG_MESSAGES[code], **({"evidence": evidence} if evidence else {})}


def find_duplicate(
    summary: str, project_id: str, nodes: Iterable[dict[str, Any]], *, now: datetime,
) -> dict[str, Any] | None:
    """Closest effective node in the same project whose summary is (nearly) identical."""
    target = _search_normalize(summary)
    if not target:
        return None
    cutoff = now - timedelta(days=DUPLICATE_WINDOW_DAYS)
    best: tuple[float, dict[str, Any]] | None = None
    for node in nodes:
        if node.get("project_id") != project_id or node.get("excluded"):
            continue
        created = _parse_time(node.get("created_at")) or _parse_time(node.get("occurred_at"))
        if created is not None and created < cutoff:
            continue
        candidate = _search_normalize(str(node.get("summary") or ""))
        if not candidate:
            continue
        if candidate == target:
            ratio = 1.0
        else:
            matcher = difflib.SequenceMatcher(None, target, candidate, autojunk=False)
            if matcher.real_quick_ratio() < DUPLICATE_RATIO or matcher.quick_ratio() < DUPLICATE_RATIO:
                continue
            ratio = matcher.ratio()
        if ratio >= DUPLICATE_RATIO and (best is None or ratio > best[0]):
            best = (ratio, node)
    if best is None:
        return None
    return {"node_id": best[1].get("node_id"), "similarity": round(best[0], 3),
            "occurred_at": best[1].get("occurred_at")}


def matching_projects(reference: str, projects: Iterable[dict[str, Any]]) -> list[str]:
    target = _search_normalize(reference)
    if not target:
        return []
    names = []
    for project in projects:
        labels = [project.get("name") or "", *(project.get("aliases") or [])]
        for label in labels:
            normalized = _search_normalize(str(label))
            if normalized and (target in normalized or normalized in target):
                names.append(str(project.get("name")))
                break
    return names


def classify_intent(
    payload: dict[str, Any], *, project: dict[str, Any] | None, nodes: list[dict[str, Any]],
    projects: list[dict[str, Any]], now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return blocking findings for one record-intent payload; empty means it may be written."""
    now = now or datetime.now()
    summary = str(payload.get("summary") or "").strip()
    next_action = str(payload.get("next_action") or "").strip()
    text = f"{summary}\n{next_action}"
    findings: list[dict[str, Any]] = []

    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    replay = idempotency_key and any(node.get("idempotency_key") == idempotency_key for node in nodes)
    if project and summary and not replay:
        duplicate = find_duplicate(summary, project["project_id"], nodes, now=now)
        if duplicate:
            findings.append(_finding("possible_duplicate", **duplicate))
    if len(_search_normalize(summary)) < 4 or PLACEHOLDER_PATTERN.match(summary):
        findings.append(_finding("placeholder_summary"))
    if BACKLOG_PATTERN.search(text):
        findings.append(_finding("backlog_item"))
    if INTERNAL_ADMIN_PATTERN.search(summary):
        findings.append(_finding("internal_admin"))
    if PERSONAL_PATTERN.search(text):
        findings.append(_finding("personal_or_mechanism"))
    occurred = _parse_time(payload.get("occurred_at"))
    if occurred is not None and occurred > now + FUTURE_TOLERANCE:
        findings.append(_finding("future_occurred_at", occurred_at=occurred.isoformat()))
    statuses = {str(payload.get(key) or "") for key in ("node_status", "status", "task_status")}
    if "等待中" in statuses and CHASE_PATTERN.search(next_action):
        findings.append(_finding("waiting_as_chase"))
    reference = str(payload.get("project") or "").strip()
    if reference and not str(payload.get("project_id") or "").strip():
        names = matching_projects(reference, projects)
        if len(names) > 1:
            findings.append(_finding("ambiguous_project", candidates=names[:8]))
    return findings


def unacknowledged(findings: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    acknowledged = {str(item) for item in (payload.get("acknowledged_flags") or [])}
    return [item for item in findings if item["code"] not in acknowledged]


def plan_intent(store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve what record-intent-json would write, without writing. Mirrors WorklogStore._record_intent."""
    intent_type = str(payload.get("intent_type") or "").strip()
    if intent_type not in VALID_INTENT_TYPES:
        raise WorklogError(f"intent_type必须是：{'、'.join(sorted(VALID_INTENT_TYPES))}")
    project_ref = str(payload.get("project_id") or payload.get("project") or "").strip()
    project = store.get_project(project_ref)
    if not project:
        raise UnknownProjectError(f"项目未登记：{project_ref}。请先确认归属，不要静默新建相似项目。")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise WorklogError("工作进展不能为空")
    source = normalize_source(payload.get("source"))

    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if idempotency_key:
        existing = next((node for node in store.list_nodes(effective_only=False)
                         if node.get("idempotency_key") == idempotency_key), None)
        if existing:
            return {"action": "idempotent_replay", "project": project["name"], "node_id": existing.get("node_id")}

    task_ref = str(payload.get("task_id") or payload.get("task_title") or "").strip()
    task = store.get_task(task_ref, project["project_id"]) if task_ref else None
    task_plan: dict[str, Any]
    if task:
        task_plan = {"action": "update", "task_id": task["task_id"], "title": task.get("title")}
    elif intent_type in {"新任务", "支线", "单次事项"}:
        task_plan = {"action": "create",
                     "title": str(payload.get("task_title") or summary.splitlines()[0]).strip()[:100],
                     "track_type": "支线" if intent_type == "支线" else str(payload.get("track_type") or "主线")}
    else:
        open_tasks = store.list_tasks(project["project_id"], OPEN_TASK_STATUSES)
        if len(open_tasks) == 1 and not task_ref:
            task = open_tasks[0]
            task_plan = {"action": "update", "task_id": task["task_id"], "title": task.get("title")}
        else:
            choices = "、".join(f"{item['title']}({item['task_id']})" for item in open_tasks[:8]) or "无"
            raise WorklogError(f"无法确定要更新的任务，请提供task_id。当前未完成任务：{choices}")
    kind = str(payload.get("kind") or ("产出" if intent_type == "成果" else ("状态变化" if intent_type == "状态变化" else "进展")))
    return {
        "action": "record",
        "project": project["name"],
        "project_id": project["project_id"],
        "task": task_plan,
        "node": {
            "intent_type": intent_type, "kind": kind, "source": source,
            "status": str(payload.get("node_status") or payload.get("status") or "进行中"),
            "task_status": str(payload.get("task_status") or ""),
            "occurred_at": str(payload.get("occurred_at") or ""),
            "include_in_report": payload.get("include_in_report", True),
            "summary": summary, "next_action": str(payload.get("next_action") or ""),
        },
    }


INTENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "record-intent-json",
    "type": "object",
    "required": ["intent_type", "summary", "source"],
    "anyOf": [{"required": ["project_id"]}, {"required": ["project"]}],
    "properties": {
        "intent_type": {"enum": sorted(VALID_INTENT_TYPES)},
        "project_id": {"type": "string", "description": "优先使用；来自 projects/brief/context"},
        "project": {"type": "string", "description": "项目名或别名；多项目匹配时会要求确认"},
        "task_id": {"type": "string"},
        "task_title": {"type": "string", "description": "新任务/支线/单次事项的任务名"},
        "summary": {"type": "string", "minLength": 4},
        "next_action": {"type": "string"},
        "node_status": {"enum": ["待开始", "进行中", "等待中", "阻塞", "已完成", "待确认"]},
        "task_status": {"enum": ["", "待开始", "进行中", "等待中", "阻塞", "已完成", "待确认"]},
        "kind": {"enum": ["进展", "产出", "反馈", "状态变化", "计划"]},
        "source": {"type": "string", "description": "WorkBuddy、Claude Code、Codex、Doubao Work、Qwen Office 等"},
        "occurred_at": {"type": "string", "description": "ISO 时间；默认当前时间"},
        "related_path": {"type": "string"},
        "include_in_report": {"type": "boolean"},
        "idempotency_key": {"type": "string", "description": "重试时保持不变，避免重复写入"},
        "lane_name": {"type": "string"},
        "track_type": {"enum": ["主线", "支线", "子任务"]},
        "relation_type": {"enum": ["顺序", "分叉", "子任务", "汇合"]},
        "predecessor_ids": {"type": "array", "items": {"type": "string"}},
        "keep_open": {"type": "boolean"},
        "acknowledged_flags": {"type": "array", "items": {"enum": sorted(FLAG_MESSAGES)},
                               "description": "用户确认后，列出要放行的预检代码"},
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "record-intent-json": {
        "schema": INTENT_SCHEMA,
        "example": {
            "intent_type": "更新任务", "project_id": "prj-xxxxxxxxxx", "task_id": "task-xxxxxxxxxxxxxxxx",
            "summary": "10/12 第二期培训按期开展，观看 160 人，录播已上传。",
            "next_action": "10/14 前完成参训统计", "node_status": "进行中", "source": "Claude Code",
            "idempotency_key": "training-phase2-20261012",
        },
    },
    "revise-node-json": {
        "schema": {
            "type": "object", "required": ["node_id", "expected_hash", "changes"],
            "properties": {
                "node_id": {"type": "string"},
                "expected_hash": {"type": "string", "description": "node --node-id 返回的 hash"},
                "changes": {"type": "object", "description": "要修订的节点字段，如 summary、next_action、status、occurred_at"},
            },
        },
        "example": {"node_id": "node-20261012104820-xxxxxxxx", "expected_hash": "<64位hash>",
                    "changes": {"summary": "更正后的摘要"}},
    },
    "exclude-node-json": {
        "schema": {
            "type": "object", "required": ["node_id", "expected_hash", "reason"],
            "properties": {"node_id": {"type": "string"}, "expected_hash": {"type": "string"},
                           "reason": {"type": "string", "minLength": 1}},
        },
        "example": {"node_id": "node-20261012104820-xxxxxxxx", "expected_hash": "<64位hash>",
                    "reason": "个人探索误挂业务项目"},
    },
}
