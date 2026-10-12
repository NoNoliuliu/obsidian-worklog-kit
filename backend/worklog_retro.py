"""Read-only stage retrospective: gather one work line's evidence and draft a method candidate.

Nothing is written. The draft follows ``propose-method-json`` so the assistant can
fill in the conclusion, show it to the user and submit it only after confirmation.
"""
from __future__ import annotations

from typing import Any

from worklog_base import UnknownProjectError, WorklogError


SIGNAL_KINDS = {"反馈", "状态变化"}
SIGNAL_STATUSES = {"阻塞", "等待中", "待确认"}


def build_retro(
    store: Any, project_ref: str, *, lane: str = "", task_id: str = "", since: str = "", summary_chars: int = 120,
    limit: int = 30,
) -> dict[str, Any]:
    project = store.get_project(project_ref)
    if not project:
        raise UnknownProjectError(f"项目未登记：{project_ref}")
    nodes = sorted(store.list_nodes(project_id=project["project_id"]), key=lambda node: str(node.get("occurred_at") or ""))
    if task_id:
        nodes = [node for node in nodes if node.get("task_id") == task_id]
    if lane:
        nodes = [node for node in nodes if lane in {node.get("lane_name"), node.get("lane_id")}]
    if since:
        nodes = [node for node in nodes if str(node.get("occurred_at") or "")[:10] >= since]
    if not nodes:
        raise WorklogError("所选范围没有有效工作节点；请检查 --lane / --task-id / --since")
    lanes = sorted({str(node.get("lane_name") or "主线") for node in nodes})
    total = len(nodes)
    first_at = nodes[0].get("occurred_at")
    nodes = nodes[-limit:] if limit > 0 else nodes
    issues = store.list_issues(project["project_id"])

    def clip(text: Any) -> str:
        value = str(text or "").strip()
        return value if len(value) <= summary_chars else value[:summary_chars].rstrip() + "…"

    timeline = [
        {"node_id": node.get("node_id"), "occurred_at": node.get("occurred_at"), "kind": node.get("kind"),
         "status": node.get("status"), "lane": node.get("lane_name"), "summary": clip(node.get("summary")),
         "next_action": clip(node.get("next_action"))}
        for node in nodes
    ]
    signals = [item for item, node in zip(timeline, nodes)
               if node.get("kind") in SIGNAL_KINDS or node.get("status") in SIGNAL_STATUSES]
    evidence_paths = sorted({str(node.get("related_path")) for node in nodes if node.get("related_path")})
    scope = lane or (lanes[0] if len(lanes) == 1 else "") or task_id or "本阶段"
    return {
        "project": project["name"],
        "scope": {"lane": lane, "task_id": task_id, "since": since, "lanes": lanes,
                  "from": first_at, "to": timeline[-1]["occurred_at"], "node_count": total,
                  "timeline_count": len(timeline), "omitted_older": total - len(timeline)},
        "timeline": timeline,
        "signals": signals,
        "issues": [
            {key: issue.get(key) for key in ("issue_id", "status", "stage", "decision", "result") if key in issue}
            | {"title": str(issue.get("path", "")).rsplit("/", 1)[-1].removesuffix(".md")}
            for issue in issues
        ],
        "checklist": [
            "这一阶段最终达成了什么，和最初计划有何偏差？",
            "哪些反馈、阻塞或返工最耗时，根因是什么？",
            "哪一步做法下次可以直接复用，触发条件和例外是什么？",
            "证据是否足以支持结论？只有一次经历的结论标为待验证。",
        ],
        "candidate_draft": {
            "project_id": project["project_id"],
            "title": f"{scope}复盘：（待命名）",
            "summary": "（待提炼：一句话说明做法和结果）",
            "applicable_scenarios": "（待补充：触发条件）",
            "steps": "\n".join(f"{index}. （待提炼）" for index in range(1, 4)),
            "anti_pattern": "（待补充：不适用情形）",
            "source_issue_ids": [issue.get("issue_id") for issue in issues if issue.get("issue_id")],
            "evidence_paths": evidence_paths,
        },
        "note": "只读草稿，未写入。提炼结论并经用户确认后，用 propose-method-json 提交 candidate_draft。",
    }
