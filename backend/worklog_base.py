"""Shared constants, errors and file helpers for the worklog store modules."""
from __future__ import annotations

import hashlib
import copy
import functools
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from collections import Counter, defaultdict
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import yaml

from worklog_structure import (
    DAILY_CATEGORIES, TRAINING_CATEGORIES, audit_structure, completed_project_findings,
    default_project_parent, finding, project_structure_findings,
)

try:
    import fcntl
    msvcrt = None
except ImportError:  # pragma: no cover - Windows deployment
    fcntl = None
    import msvcrt


MANAGED_START = "<!-- weekly-system:start -->"
MANAGED_END = "<!-- weekly-system:end -->"
DOMAIN_START = "<!-- work-domain:start -->"
DOMAIN_END = "<!-- work-domain:end -->"
MATERIAL_START = "<!-- project-materials:start -->"
MATERIAL_END = "<!-- project-materials:end -->"
VALID_STATUSES = {"待开始", "进行中", "等待中", "阻塞", "已完成", "待确认"}
VALID_PROJECT_STATUSES = {"进行中", "已完成", "暂停", "待确认"}
VALID_TRACK_TYPES = {"主线", "支线", "子任务"}
VALID_RELATION_TYPES = {"顺序", "分叉", "子任务", "汇合"}
VALID_KINDS = {"进展", "产出", "反馈", "状态变化", "计划"}
VALID_SOURCES = {"WorkBuddy", "Claude Code", "Codex", "Doubao Work", "Qwen Office", "QoderWork",
                 "手工记录", "历史Excel", "Obsidian历史"}
SOURCE_ALIASES = {"Doubao": "Doubao Work", "豆包": "Doubao Work", "Qwen": "Qwen Office",
                  "千问办公": "Qwen Office", "Claude": "Claude Code"}


def normalize_source(value: Any) -> str:
    """Canonical AI tool name for new records; unknown names are rejected, not stored loosely."""
    raw = str(value or "").strip() or "手工记录"
    source = SOURCE_ALIASES.get(raw, raw)
    if source not in VALID_SOURCES:
        raise WorklogError(f"source 必须是：{'、'.join(sorted(VALID_SOURCES))}（收到：{raw}）")
    return source
VALID_WORK_TYPES = {"项目", "持续工作", "单次事项"}
VALID_PROJECT_MODES = {"bounded", "ongoing"}
VALID_INTENT_TYPES = {"更新任务", "新任务", "支线", "成果", "状态变化", "单次事项"}


def _separate_domain_block(text: str) -> str:
    """Migrate legacy domain summaries without touching project status blocks."""
    pattern = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), re.S)
    return pattern.sub(lambda match: match.group(0).replace(MANAGED_START, DOMAIN_START).replace(MANAGED_END, DOMAIN_END)
                       if "## 当前体系事项" in match.group(0) else match.group(0), text)
VALID_ISSUE_STATUSES = {"待分析", "处理中", "已解决", "已接受", "待确认"}
VALID_METHOD_STATUSES = {"候选", "正式", "已归档"}
NODE_INDEX_VERSION = 1
_TIMING_STORE = ContextVar("worklog_timing_store", default=None)
_INTENT_WRITE_STORE = ContextVar("worklog_intent_write_store", default=None)


def _classify_project(name: str, major_work: str, overview_path: str = "") -> dict[str, Any]:
    """Apply generic default categories; customize for your own Vault."""
    if overview_path.startswith("项目管理/"):
        return {"work_domain": "项目管理", "work_category": "其他项目",
                "work_type": "项目", "project_mode": "bounded", "view_tags": ["项目推进"]}
    if major_work == "培训工作" or "培训" in name:
        return {"work_domain": "培训工作", "work_category": "培训实施",
                "work_type": "项目", "project_mode": "bounded", "view_tags": ["培训工作"]}
    return {"work_domain": "日常与专项工作", "work_category": "临时支持",
            "work_type": "单次事项", "project_mode": "ongoing", "view_tags": [major_work or "其他"]}


class WorklogError(RuntimeError):
    pass


class UnknownProjectError(WorklogError):
    pass


class ConflictError(WorklogError):
    pass


# Classification copied onto each node so Obsidian Bases can group nodes without joins.
NODE_PROJECT_FIELDS = ("work_domain", "work_category", "work_type", "view_tags")
# Project-level text no longer copied onto new nodes (1.35); refreshed only where old nodes still carry it.
LEGACY_NODE_PROJECT_FIELDS = ("major_work", "key_point")


def node_project_fields(project: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project-derived node frontmatter; legacy fields are kept current only if the node already has them."""
    fields: dict[str, Any] = {
        "project_name": project["name"],
        "work_domain": project.get("work_domain", ""),
        "work_category": project.get("work_category", ""),
        "work_type": project.get("work_type", "项目"),
        "view_tags": project.get("view_tags", []),
    }
    for key in LEGACY_NODE_PROJECT_FIELDS:
        if existing is not None and key in existing:
            fields[key] = project.get(key, "")
    return fields


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _normalize(text: str) -> str:
    return re.sub(r"[\s\-_—&（）()]+", "", (text or "").lower())


def _search_normalize(text: str) -> str:
    """Make short natural-language queries tolerant of common Chinese connectors."""
    return re.sub(r"[的与和及、]", "", _normalize(text))


def _safe_filename(text: str, fallback: str, max_length: int = 80) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", cleaned).strip(" .-")
    return cleaned[:max_length] or fallback


def _safe_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()


def _atomic_write(path: Path, content: str) -> None:
    store = _TIMING_STORE.get()
    with store.timed_stage("_atomic_write") if store is not None else nullcontext():
        path.parent.mkdir(parents=True, exist_ok=True)
        intent_store = _INTENT_WRITE_STORE.get()
        if intent_store is not None and path.exists() and intent_store._is_managed_intent_path(path):
            intent_store._write_existing_node_without_replace(path, content)
            return
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)


def _write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    _atomic_write(path, content)
    return True


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    return _parse_frontmatter_text(path.read_text(encoding="utf-8"))


def _parse_frontmatter_text(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not match:
        return {}, text
    parsed = yaml.safe_load(match.group(1)) or {}

    def stringify_dates(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, list):
            return [stringify_dates(item) for item in value]
        if isinstance(value, dict):
            return {key: stringify_dates(item) for key, item in value.items()}
        return value

    return stringify_dates(parsed), match.group(2)


def _extract_section(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def _clean_cell(text: str) -> str:
    return (text or "").replace("|", "｜").replace("\r", " ").replace("\n", "；").strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() not in {"false", "0", "no", "否", "none", ""}


def _timed_method(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        token = _TIMING_STORE.set(self) if self._timing_enabled else None
        try:
            with self.timed_stage(method.__name__):
                return method(self, *args, **kwargs)
        finally:
            if token is not None:
                _TIMING_STORE.reset(token)
    return wrapped


@dataclass(frozen=True)
class WeekContext:
    anchor: date
    calendar_start: date
    calendar_end: date
    start: date
    end: date
    workdays: tuple[date, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor.isoformat(),
            "calendar_start": self.calendar_start.isoformat(),
            "calendar_end": self.calendar_end.isoformat(),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "workdays": [item.isoformat() for item in self.workdays],
            "is_last_workday": date.today() == self.end,
        }
