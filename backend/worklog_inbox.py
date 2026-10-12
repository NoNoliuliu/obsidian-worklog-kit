"""Inbox for unconfirmed captures and the personal note area.

``capture`` drops a quick note into ``00-收件箱/`` (or ``个人/随手记/`` with
``personal=True``) without touching projects, tasks, nodes, panorama or weekly
reports. ``triage`` later turns each pending item into exactly one of: a work
node (via record_intent), a method candidate, a personal note, or a discarded
item. Files are never deleted; status and outcome are kept in frontmatter.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from worklog_base import (
    WorklogError, _atomic_write, _now, _parse_frontmatter, _safe_filename, _safe_yaml, normalize_source,
)


INBOX_DIR = "00-收件箱"
PERSONAL_NOTES_DIR = "个人/随手记"
PENDING = "待处理"
TRIAGE_STATUS = {"node": "已转节点", "candidate": "已转候选", "personal": "已转个人", "discard": "已丢弃"}


def inbox_dir(store: Any) -> Path:
    return store.vault_root / INBOX_DIR


def personal_dir(store: Any) -> Path:
    return store.vault_root / PERSONAL_NOTES_DIR


def _title(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:40] or "随手记"


def capture(
    store: Any, text: str, *, source: str = "", project_hint: str = "", tags: list[str] | None = None,
    personal: bool = False,
) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise WorklogError("收件内容不能为空")
    now = _now()
    item_id = f"inbox-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    title = _title(text)
    meta = {
        "type": "personal-note" if personal else "inbox-item",
        "inbox_id": item_id,
        "status": "个人笔记" if personal else PENDING,
        "captured_at": now.isoformat(),
        "source": normalize_source(source),
        "project_hint": str(project_hint or "").strip(),
        "tags": sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()}),
    }
    folder = personal_dir(store) if personal else inbox_dir(store)
    path = folder / f"{now.strftime('%Y%m%d-%H%M%S')}-{_safe_filename(title, '随手记', 40)}-{item_id[-6:]}.md"
    with store.mutation_lock():
        _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n# {title}\n\n{text}\n")
    return {**meta, "path": path.relative_to(store.vault_root).as_posix(), "title": title}


def _read_item(store: Any, path: Path) -> dict[str, Any] | None:
    meta, body = _parse_frontmatter(path)
    if meta.get("type") not in {"inbox-item", "personal-note"}:
        return None
    lines = [line for line in body.strip().splitlines() if not line.startswith("# ")]
    return {**meta, "path": path.relative_to(store.vault_root).as_posix(), "text": "\n".join(lines).strip()}


def list_inbox(store: Any, *, status: str = PENDING) -> list[dict[str, Any]]:
    folder = inbox_dir(store)
    if not folder.is_dir():
        return []
    items = [item for path in sorted(folder.glob("*.md")) if (item := _read_item(store, path))]
    if status:
        items = [item for item in items if item.get("status") == status]
    return sorted(items, key=lambda item: str(item.get("captured_at") or ""))


def get_item(store: Any, inbox_id: str) -> dict[str, Any]:
    matches = [item for item in list_inbox(store, status="") if item.get("inbox_id") == inbox_id]
    if len(matches) != 1:
        raise WorklogError(f"收件箱条目不存在：{inbox_id}")
    return matches[0]


def triage(
    store: Any, inbox_id: str, to: str, *, reason: str = "", payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one pending item. ``payload`` is the record-intent or propose-method JSON for node/candidate."""
    if to not in TRIAGE_STATUS:
        raise WorklogError(f"分流目标必须是：{'、'.join(TRIAGE_STATUS)}")
    with store.mutation_lock():
        item = get_item(store, inbox_id)
        if item.get("status") != PENDING:
            raise WorklogError(f"条目已处理（{item.get('status')}），不重复分流")
        if to == "discard" and not reason.strip():
            raise WorklogError("丢弃须说明理由")
        path = store.vault_root / item["path"]
        meta, body = _parse_frontmatter(path)
        outcome: dict[str, Any] = {"to": to, "at": _now().isoformat(), "reason": reason.strip()}
        result: dict[str, Any] = {}
        if to == "node":
            if not payload:
                raise WorklogError("转为工作节点需要 record-intent JSON")
            result = store.record_intent(payload)
            outcome["node_id"] = result["node"]["node_id"]
        elif to == "candidate":
            if not payload:
                raise WorklogError("转为方法候选需要 propose-method JSON")
            result = store.propose_method(payload)
            outcome["candidate_id"] = result.get("candidate_id")
        meta["status"] = TRIAGE_STATUS[to]
        meta["triage"] = outcome
        if to == "personal":
            meta["type"] = "personal-note"
            target = personal_dir(store) / path.name
            if target.exists():
                raise WorklogError(f"个人笔记已存在同名文件：{target.name}")
            _atomic_write(target, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
            os.remove(path)
            outcome["path"] = target.relative_to(store.vault_root).as_posix()
        else:
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
    return {"inbox_id": inbox_id, "status": meta["status"], "triage": outcome, **({"result": result} if result else {})}


def inbox_counts(store: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in list_inbox(store, status=""):
        counts[str(item.get("status"))] = counts.get(str(item.get("status")), 0) + 1
    return counts


def pending_age_days(item: dict[str, Any], now: datetime | None = None) -> int:
    try:
        captured = datetime.fromisoformat(str(item.get("captured_at")))
    except ValueError:
        return 0
    return max(0, ((now or datetime.now()) - captured).days)
