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
from typing import Any, Callable, Iterable

import yaml

from worklog_structure import (
    DAILY_CATEGORIES, TRAINING_CATEGORIES, audit_structure, completed_project_findings,
    default_project_parent, finding, project_structure_findings,
)

try:
    import fcntl
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
VALID_SOURCES = {"WorkBuddy", "Claude Code", "Codex", "QoderWork", "手工记录", "历史Excel", "Obsidian历史"}
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


class WorklogStore:
    def __init__(
        self, vault_root: str | Path, *, read_only: bool = False,
        node_index_path: str | Path | None = None,
        entity_index_path: str | Path | None = None,
    ):
        self.vault_root = Path(vault_root)
        self.root = self.vault_root / "周报系统"
        self.projects_dir = self.root / "项目台账"
        self.tasks_dir = self.root / "任务台账"
        self.nodes_dir = self.root / "工作节点"
        self.drafts_dir = self.root / "周报草稿"
        self.backups_dir = self.root / "备份"
        self.canvas_dir = self.root / "项目关系图"
        self.views_dir = self.root / "视图"
        self.method_dir = self.vault_root / "方法论库"
        self.panorama_dir = self.vault_root / "00-工作全景"
        self.win_dir = self.vault_root / "培训工作"
        self.daily_dir = self.vault_root / "日常与专项工作"
        self.calendar_path = self.root / "工作日历.yaml"
        self.schema_path = self.root / "结构版本.yaml"
        self.read_only = read_only
        self.operation_warnings: list[dict[str, Any]] = []
        self._warning_project_ids: set[str] = set()
        self.lock_path = self.root / ".worklog.lock"
        self._lock = threading.RLock()
        self._mutation_depth = 0
        self._project_cache: dict[str, dict[str, Any]] = {}
        self._task_cache: dict[str, dict[str, Any]] = {}
        self._node_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._all_nodes_snapshot: list[dict[str, Any]] | None = None
        self._node_snapshot_depth = 0
        self._timing_enabled = False
        self._timings: dict[str, dict[str, float | int]] = {}
        self.node_index_path = Path(node_index_path) if node_index_path else None
        self._persistent_node_index: dict[str, dict[str, Any]] | None = None
        self._persistent_node_index_dirty = False
        self.entity_index_path = Path(entity_index_path) if entity_index_path else None
        self._entity_entries: dict[str, dict[str, Any]] | None = None
        self._entity_dirty = False
        self._ensure_layout()

    def _load_entity_index(self) -> dict[str, dict[str, Any]]:
        if self._entity_entries is None:
            self._entity_entries = {}
            if self.entity_index_path and self.entity_index_path.exists():
                with self.timed_stage("entity_index_load"):
                    try:
                        payload = json.loads(self.entity_index_path.read_text(encoding="utf-8"))
                        if (payload.get("version") == 1
                                and payload.get("vault_root") == str(self.vault_root.resolve())
                                and isinstance(payload.get("entries"), dict)):
                            self._entity_entries = payload["entries"]
                    except (OSError, ValueError, AttributeError):
                        pass
        return self._entity_entries

    @_timed_method
    def flush_entity_index(self) -> None:
        if not self.entity_index_path or not self._entity_dirty:
            return
        with self.timed_stage("entity_index_flush"):
            _atomic_write(self.entity_index_path, json.dumps({
                "version": 1, "vault_root": str(self.vault_root.resolve()),
                "entries": self._entity_entries,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._entity_dirty = False

    @staticmethod
    def _entity_signature(path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]

    def _cached_entity(
        self, path: Path, kind: str, reader: Callable[[Path], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        entries = self._load_entity_index()
        key = path.relative_to(self.root).as_posix()
        signature = self._entity_signature(path)
        memory = self._project_cache if kind == "project" else self._task_cache
        cached = memory.get(str(path)) or entries.get(key)
        item = cached.get("item") if isinstance(cached, dict) else None
        valid_item = item is None or (
            isinstance(item, dict) and item.get("path") == str(path)
            and item.get("type") == {"project": "weekly-project", "task": "work-task"}[kind]
            and isinstance(item.get("hash"), str) and len(item["hash"]) == 64
        )
        if (isinstance(cached, dict) and cached.get("signature") == signature
                and cached.get("kind") == kind and "item" in cached
                and valid_item):
            memory[str(path)] = cached
            return copy.deepcopy(cached["item"])
        item = reader(path)
        # Never label a read raced by an external edit as a valid cached snapshot.
        if signature == self._entity_signature(path):
            entries[key] = {"signature": signature, "kind": kind, "item": copy.deepcopy(item)}
            memory[str(path)] = entries[key]
        else:
            entries.pop(key, None)
            memory.pop(str(path), None)
        self._entity_dirty = True
        return item

    def _prune_entities(self, paths: list[Path], kind: str) -> None:
        entries = self._load_entity_index()
        active = {path.relative_to(self.root).as_posix() for path in paths}
        memory = self._project_cache if kind == "project" else self._task_cache
        active_paths = {str(path) for path in paths}
        for key in set(memory) - active_paths:
            memory.pop(key)
        for key, value in list(entries.items()):
            if not isinstance(value, dict) or (value.get("kind") == kind and key not in active):
                entries.pop(key)
                self._entity_dirty = True

    def _load_node_index(self) -> dict[str, dict[str, Any]]:
        if self._persistent_node_index is not None:
            return self._persistent_node_index
        entries: dict[str, dict[str, Any]] = {}
        if self.node_index_path and self.node_index_path.exists():
            with self.timed_stage("node_index_load"):
                try:
                    payload = json.loads(self.node_index_path.read_text(encoding="utf-8"))
                    if payload.get("version") == NODE_INDEX_VERSION and payload.get("vault_root") == str(self.vault_root):
                        cached_entries = payload.get("entries") or {}
                        entries = cached_entries if isinstance(cached_entries, dict) else {}
                except (OSError, json.JSONDecodeError, AttributeError):
                    entries = {}
        self._persistent_node_index = entries
        return entries

    def flush_node_index(self) -> None:
        if not self.node_index_path or not self._persistent_node_index_dirty:
            return
        with self.timed_stage("node_index_flush"):
            payload = {
                "version": NODE_INDEX_VERSION,
                "vault_root": str(self.vault_root),
                "entries": self._persistent_node_index or {},
            }
            _atomic_write(
                self.node_index_path,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            )
            self._persistent_node_index_dirty = False

    def _node_index_key(self, path: Path) -> str:
        try:
            return path.relative_to(self.nodes_dir).as_posix()
        except ValueError:
            return str(path)

    def enable_timing(self, enabled: bool = True) -> None:
        self._timing_enabled = enabled

    @contextmanager
    def timed_stage(self, name: str):
        if not self._timing_enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            current = self._timings.setdefault(name, {"seconds": 0.0, "calls": 0})
            current["seconds"] = float(current["seconds"]) + elapsed
            current["calls"] = int(current["calls"]) + 1

    def timing_report(self) -> dict[str, Any]:
        return {
            name: {"seconds": round(float(value["seconds"]), 4), "calls": int(value["calls"])}
            for name, value in self._timings.items()
        }

    @contextmanager
    def node_snapshot_scope(self):
        outermost = self._node_snapshot_depth == 0
        self._node_snapshot_depth += 1
        if outermost:
            self._all_nodes_snapshot = None
        try:
            yield
        finally:
            self._node_snapshot_depth -= 1
            if outermost:
                self._all_nodes_snapshot = None

    def _invalidate_node_cache(self, path: Path | None = None) -> None:
        self._all_nodes_snapshot = None
        if path is None:
            self._node_cache.clear()
        else:
            self._node_cache.pop(str(path), None)

    def _refresh_node_cache_entry(self, path: Path) -> dict[str, Any] | None:
        """Refresh one changed node without discarding the command-wide snapshot."""
        try:
            stat = path.stat()
            item = self._read_node(path)
        except (OSError, UnicodeDecodeError):
            item = None
        key = str(path)
        if item:
            self._node_cache[key] = (stat.st_mtime_ns, stat.st_size, dict(item))
        else:
            self._node_cache.pop(key, None)
        if self._node_snapshot_depth and self._all_nodes_snapshot is not None:
            retained = [row for row in self._all_nodes_snapshot if row.get("path") != key]
            if item:
                retained.append(dict(item))
            self._all_nodes_snapshot = retained
        else:
            self._all_nodes_snapshot = None
        if self.node_index_path:
            persistent = self._load_node_index()
            index_key = self._node_index_key(path)
            if item:
                persistent[index_key] = {
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                    "size": stat.st_size,
                    "item": dict(item),
                }
            else:
                persistent.pop(index_key, None)
            self._persistent_node_index_dirty = True
            if not self._node_snapshot_depth:
                self.flush_node_index()
        return dict(item) if item else None

    def _ensure_layout(self) -> None:
        if not self.vault_root.exists():
            raise WorklogError(f"Obsidian Vault 不存在：{self.vault_root}")
        if self.read_only:
            required = (self.root, self.projects_dir, self.tasks_dir, self.nodes_dir)
            missing = [str(path) for path in required if not path.exists()]
            if missing:
                raise WorklogError(f"只读查询所需目录不存在：{', '.join(missing)}")
            return
        for folder in (
            self.projects_dir, self.tasks_dir, self.nodes_dir, self.drafts_dir,
            self.backups_dir, self.canvas_dir, self.views_dir,
            self.panorama_dir, self.win_dir, self.daily_dir, self.method_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        if not self.calendar_path.exists():
            _atomic_write(
                self.calendar_path,
                "# 默认周一至周五。节假日和调休日期可在网页中维护。\n"
                "holidays: []\nworkdays: []\n",
            )
        self._write_base_views()

    @contextmanager
    def mutation_lock(self):
        """Serialize a complete read-compute-write operation across AI processes."""
        if self.read_only:
            raise WorklogError("只读模式不允许写入")
        if self._mutation_depth:
            self._mutation_depth += 1
            try:
                yield
            finally:
                self._mutation_depth -= 1
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_started = time.perf_counter()
        with self._lock, self.lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows deployment
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            if self._timing_enabled:
                elapsed = time.perf_counter() - lock_started
                current = self._timings.setdefault("lock_wait", {"seconds": 0.0, "calls": 0})
                current["seconds"] = float(current["seconds"]) + elapsed
                current["calls"] = int(current["calls"]) + 1
            self._mutation_depth = 1
            try:
                yield
            finally:
                self._mutation_depth = 0
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows deployment
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _project_path(self, name: str) -> Path:
        return self.projects_dir / f"{_safe_filename(name, '未命名项目')}.md"

    def _task_path(self, project_name: str, title: str) -> Path:
        return self.tasks_dir / _safe_filename(project_name, "未命名项目") / f"{_safe_filename(title, '未命名任务')}.md"

    def _node_path(self, occurred_at: datetime, node_id: str, project_name: str, summary: str) -> Path:
        folder = self.nodes_dir / f"{occurred_at:%Y}" / f"{occurred_at:%m}"
        stem = f"{occurred_at:%Y%m%d-%H%M}-{_safe_filename(project_name, '未命名项目', 30)}-{_safe_filename(summary, '工作记录', 60)}"
        candidate = folder / f"{stem}.md"
        if candidate.exists():
            candidate = folder / f"{stem}—{node_id[-6:]}.md"
        return candidate

    def _canvas_path(self, project_name: str) -> Path:
        return self.canvas_dir / f"{_safe_filename(project_name, '未命名项目')}-工作关系.canvas"

    def _write_base_views(self) -> None:
        project_base = """filters:
  and:
    - 'type == "weekly-project"'
    - 'display_in_views != false'
    - 'file.inFolder("周报系统/项目台账")'
formulas:
  latest: 'if(latest_summary, latest_summary, "暂无记录")'
  updated: 'if(last_activity_at, date(last_activity_at).format("YYYY-MM-DD HH:mm"), "")'
properties:
  name:
    displayName: "项目"
  major_work:
    displayName: "主要工作"
  key_point:
    displayName: "关键点"
  project_status:
    displayName: "项目状态"
  formula.latest:
    displayName: "最新进度"
  formula.updated:
    displayName: "最近更新"
views:
  - type: table
    name: "项目台账"
    groupBy:
      property: major_work
      direction: ASC
    order:
      - name
      - project_status
      - key_point
      - formula.latest
      - formula.updated
"""
        review_base = """filters:
  and:
    - 'type == "weekly-node"'
    - 'excluded != true'
    - 'review_state == "待确认"'
formulas:
  happened: 'date(occurred_at).format("YYYY-MM-DD HH:mm")'
properties:
  project_name:
    displayName: "项目"
  status:
    displayName: "节点状态"
  track_type:
    displayName: "工作线类型"
  lane_name:
    displayName: "工作线"
  include_in_report:
    displayName: "纳入周报"
  review_state:
    displayName: "审核状态"
  formula.happened:
    displayName: "发生时间"
views:
  - type: table
    name: "待确认节点"
    order:
      - project_name
      - formula.happened
      - status
      - track_type
      - lane_name
      - include_in_report
      - review_state
"""
        task_base = """filters:
  and:
    - 'type == "work-task"'
    - 'file.inFolder("周报系统/任务台账")'
formulas:
  updated: 'date(updated_at).format("YYYY-MM-DD HH:mm")'
properties:
  project_name:
    displayName: "所属工作"
  title:
    displayName: "任务"
  status:
    displayName: "状态"
  work_type:
    displayName: "工作类型"
  track_type:
    displayName: "任务线"
  formula.updated:
    displayName: "最近更新"
views:
  - type: table
    name: "任务台账"
    groupBy:
      property: project_name
      direction: ASC
    order:
      - project_name
      - title
      - status
      - track_type
      - formula.updated
"""
        for path, content in (
            (self.views_dir / "项目台账.base", project_base),
            (self.views_dir / "待确认节点.base", review_base),
            (self.views_dir / "任务台账.base", task_base),
        ):
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                _atomic_write(path, content)

    @_timed_method
    def _read_project(self, path: Path) -> dict[str, Any] | None:
        raw = path.read_bytes()
        meta, _ = _parse_frontmatter_text(raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))
        if meta.get("type") != "weekly-project":
            return None
        meta["path"] = str(path)
        meta["hash"] = hashlib.sha256(raw).hexdigest()
        meta["aliases"] = meta.get("aliases") or []
        meta["project_status"] = meta.get("project_status") or meta.get("status") or "进行中"
        classification = _classify_project(
            str(meta.get("name", "")), str(meta.get("major_work", "")), str(meta.get("overview_path", ""))
        )
        for key in ("work_domain", "work_category", "work_type", "project_mode", "view_tags"):
            meta[key] = meta.get(key) or classification[key]
        return meta

    @_timed_method
    def list_projects(self) -> list[dict[str, Any]]:
        paths = sorted(self.projects_dir.glob("*.md"))
        self._prune_entities(paths, "project")
        projects = [self._cached_entity(path, "project", self._read_project) for path in paths]
        return [item for item in projects if item is not None]

    @_timed_method
    def get_project(self, project_id_or_name: str) -> dict[str, Any] | None:
        target = _normalize(project_id_or_name)
        for project in self.list_projects():
            candidates = [project.get("project_id", ""), project.get("name", ""), *(project.get("aliases") or [])]
            if any(_normalize(value) == target for value in candidates):
                return project
        return None

    def create_project(
        self,
        *,
        name: str,
        major_work: str,
        key_point: str,
        status: str = "进行中",
        aliases: Iterable[str] | None = None,
        overview_path: str = "",
        create_overview: bool = False,
        work_domain: str = "",
        work_category: str = "",
        work_type: str = "",
        project_mode: str = "",
        view_tags: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if not name.strip() or not major_work.strip() or not key_point.strip():
            raise WorklogError("新项目必须填写项目名称、主要工作和关键点")
        if work_type and work_type not in VALID_WORK_TYPES:
            raise WorklogError(f"无效工作类型：{work_type}")
        if project_mode and project_mode not in VALID_PROJECT_MODES:
            raise WorklogError(f"无效项目模式：{project_mode}")
        if work_category and (work_category in {".", ".."} or any(c in work_category for c in "/\\")):
            raise WorklogError("工作分类必须是单个分类名称，不能包含路径分隔符")
        existing = self.get_project(name)
        if existing:
            return existing
        project_id = f"prj-{hashlib.sha1(_normalize(name).encode('utf-8')).hexdigest()[:10]}"
        now = _now().isoformat()
        classification = _classify_project(name.strip(), major_work.strip(), overview_path.replace("\\", "/"))
        effective_domain = work_domain or classification["work_domain"]
        effective_category = work_category or classification["work_category"]
        if create_overview:
            if not overview_path:
                safe_name = re.sub(r'[<>:"/\\|?*]+', "-", name.strip()).strip(". ")
                overview = (self.vault_root / default_project_parent(effective_domain, effective_category)
                            / safe_name / f"{safe_name}-项目总览.md")
                overview_path = overview.relative_to(self.vault_root).as_posix()
            else:
                candidate = Path(overview_path)
                overview = candidate if candidate.is_absolute() else self.vault_root / candidate
            if not overview.exists():
                _atomic_write(
                    overview,
                    "---\n"
                    f"项目名称: {name.strip()}\n"
                    f"状态: {status if status in VALID_STATUSES else '进行中'}\n"
                    "---\n\n"
                    f"# {name.strip()}\n\n"
                    "## 项目背景\n\n由周报系统创建，后续可在此补充项目背景、流程和产出。\n",
                )
        meta = {
            "type": "weekly-project",
            "project_id": project_id,
            "name": name.strip(),
            "aliases": sorted({item.strip() for item in (aliases or []) if item.strip()}),
            "major_work": major_work.strip(),
            "key_point": key_point.strip(),
            "project_status": status if status in VALID_PROJECT_STATUSES else "进行中",
            "overview_path": overview_path.replace("\\", "/"),
            "work_domain": effective_domain,
            "work_category": effective_category,
            "work_type": work_type if work_type in VALID_WORK_TYPES else classification["work_type"],
            "project_mode": project_mode if project_mode in VALID_PROJECT_MODES else classification["project_mode"],
            "view_tags": sorted({*(view_tags or []), *classification["view_tags"]}),
            "created_at": now,
            "updated_at": now,
        }
        body = f"# {meta['name']}\n\n由周报系统维护的项目台账。详细资料见对应项目总览。\n"
        _atomic_write(self._project_path(meta["name"]), f"---\n{_safe_yaml(meta)}\n---\n\n{body}")
        self.sync_native_views(project_id)
        self.write_work_panorama_pages()
        self.operation_warnings.extend(project_structure_findings(self.vault_root, meta))
        self._warning_project_ids.add(project_id)
        return self.get_project(project_id) or meta

    @_timed_method
    def update_project(self, project_id: str, changes: dict[str, Any], *, sync_overview: bool = True) -> dict[str, Any]:
        project = self.get_project(project_id)
        if not project:
            raise UnknownProjectError(f"项目不存在：{project_id}")
        previous = copy.deepcopy(project)
        path = Path(project.pop("path"))
        project.pop("hash", None)
        structural_change = any(
            key in changes and changes[key] != project.get(key)
            for key in ("name", "major_work", "key_point", "work_domain", "work_category", "work_type", "view_tags")
        )
        if "status" in changes and "project_status" not in changes:
            changes = {**changes, "project_status": changes["status"]}
        for key in (
            "name", "major_work", "key_point", "project_status", "overview_path", "aliases",
            "work_domain", "work_category", "work_type", "project_mode", "view_tags", "display_in_views",
        ):
            if key in changes:
                project[key] = changes[key]
        project.pop("status", None)
        project["updated_at"] = _now().isoformat()
        body = f"# {project['name']}\n\n由周报系统维护的项目台账。详细资料见对应项目总览。\n"
        _atomic_write(path, f"---\n{_safe_yaml(project)}\n---\n\n{body}")
        self._project_cache.pop(str(path), None)
        if structural_change:
            for node in self.list_nodes(effective_only=False):
                if node.get("project_id") != project_id:
                    continue
                node_path = Path(node["path"])
                meta, node_body = _parse_frontmatter(node_path)
                meta["project_name"] = project["name"]
                meta["major_work"] = project["major_work"]
                meta["key_point"] = project["key_point"]
                meta["work_domain"] = project.get("work_domain", "")
                meta["work_category"] = project.get("work_category", "")
                meta["work_type"] = project.get("work_type", "项目")
                meta["view_tags"] = project.get("view_tags", [])
                _atomic_write(node_path, f"---\n{_safe_yaml(meta)}\n---\n\n{node_body.lstrip()}")
            self._invalidate_node_cache()
            for task in self.list_tasks(project_id):
                task_path = Path(task["path"])
                task_meta, task_body = _parse_frontmatter(task_path)
                task_meta.update({
                    "project_name": project["name"],
                    "work_type": project.get("work_type", "项目"),
                    "work_domain": project.get("work_domain", ""),
                    "work_category": project.get("work_category", ""),
                })
                _atomic_write(task_path, f"---\n{_safe_yaml(task_meta)}\n---\n\n{task_body.lstrip()}")
                self._task_cache.pop(str(task_path), None)
        if sync_overview:
            self.sync_overview(project_id)
            self.sync_native_views(project_id)
            self.write_work_panorama_pages()
        self._warning_project_ids.add(project_id)
        if previous.get("overview_path") != project.get("overview_path"):
            self.operation_warnings.append(finding(
                "overview_path_changed_without_move", project,
                "总览引用已更新，原文件和目录不会自动搬移；请核对目标文件及原位置。",
                old_path=previous.get("overview_path", ""), new_path=project.get("overview_path", "")))
        if previous.get("name") != project.get("name"):
            self.operation_warnings.append(finding(
                "project_renamed_without_file_move", project,
                "项目显示名称已更新，台账文件、任务目录和原关系图不会自动改名或清理。",
                old_name=previous["name"], new_name=project["name"], ledger_path=str(path),
                old_canvas=str(self._canvas_path(previous["name"])),
                task_directories=sorted({str(Path(t["path"]).parent) for t in self.list_tasks(project_id)})))
        if any(previous.get(key) != project.get(key) for key in ("name", "overview_path", "work_domain", "work_category")):
            self.operation_warnings.extend(project_structure_findings(self.vault_root, project))
        return self.get_project(project_id) or project

    def write_warnings(self) -> list[dict[str, Any]]:
        """Command-local diagnostics, evaluated against final state and never persisted."""
        results = list(self.operation_warnings)
        for project_id in sorted(self._warning_project_ids):
            project = self.get_project(project_id)
            if project and project.get("project_status") == "已完成":
                results.extend(completed_project_findings(project, self.list_tasks(project_id)))
        unique = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in results}
        return list(unique.values())

    def audit_structure(self) -> dict[str, Any]:
        return audit_structure(self)

    @_timed_method
    def _read_task(self, path: Path) -> dict[str, Any] | None:
        raw = path.read_bytes()
        meta, body = _parse_frontmatter_text(raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))
        if meta.get("type") != "work-task":
            return None
        meta["description"] = _extract_section(body, "任务说明")
        meta["related_path"] = _extract_section(body, "关联资料")
        if meta["description"] == "无":
            meta["description"] = ""
        if meta["related_path"] == "无":
            meta["related_path"] = ""
        meta["required"] = _as_bool(meta.get("required", True))
        meta["aliases"] = meta.get("aliases") or []
        meta["predecessor_task_ids"] = meta.get("predecessor_task_ids") or []
        meta["status_history"] = meta.get("status_history") or []
        meta["path"] = str(path)
        meta["hash"] = hashlib.sha256(raw).hexdigest()
        return meta

    @_timed_method
    def list_tasks(self, project: str = "", statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        project_meta = self.get_project(project) if project else None
        allowed = set(statuses or [])
        tasks: list[dict[str, Any]] = []
        paths = list(self.tasks_dir.rglob("*.md"))
        self._prune_entities(paths, "task")
        for path in paths:
            item = self._cached_entity(path, "task", self._read_task)
            if not item:
                continue
            if project and (not project_meta or item.get("project_id") != project_meta["project_id"]):
                continue
            if allowed and item.get("status") not in allowed:
                continue
            tasks.append(item)
        return sorted(tasks, key=lambda item: (item.get("updated_at", ""), item.get("created_at", "")), reverse=True)

    def get_task(self, task_id_or_title: str, project: str = "") -> dict[str, Any] | None:
        target = _normalize(task_id_or_title)
        matches = []
        for task in self.list_tasks(project):
            candidates = [task.get("task_id", ""), task.get("title", ""), *(task.get("aliases") or [])]
            if any(_normalize(value) == target for value in candidates):
                matches.append(task)
        if len(matches) > 1:
            raise WorklogError(f"任务名称不唯一：{task_id_or_title}，请使用task_id")
        return matches[0] if matches else None

    def tasks_with_current_progress(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expose current facts separately from manually maintained background text."""
        latest = {}
        for node in self.list_nodes():
            if node.get("task_id"):
                latest.setdefault(node["task_id"], node)
        return [{**task, "current_progress": latest.get(task["task_id"], {}).get("summary", ""),
                 "next_action": latest.get(task["task_id"], {}).get("next_action", ""),
                 "progress_at": latest.get(task["task_id"], {}).get("occurred_at", ""),
                 "description_role": "背景说明；当前进展与下一步以最新有效工作节点为准"}
                for task in tasks]

    @_timed_method
    def create_task(
        self,
        *,
        project: str,
        title: str,
        status: str = "待开始",
        description: str = "",
        aliases: Iterable[str] | None = None,
        required: bool = True,
        track_type: str = "主线",
        lane_name: str = "",
        parent_task_id: str = "",
        predecessor_task_ids: Iterable[str] | None = None,
        related_path: str = "",
        source: str = "手工记录",
        task_id: str = "",
        derived_from_history: bool = False,
    ) -> dict[str, Any]:
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        if not title.strip():
            raise WorklogError("任务名称不能为空")
        if status not in VALID_STATUSES:
            raise WorklogError(f"无效任务状态：{status}")
        if track_type not in VALID_TRACK_TYPES:
            raise WorklogError(f"无效任务线类型：{track_type}")
        existing = self.get_task(task_id or title, project_meta["project_id"])
        if existing:
            return existing
        now = _now().isoformat()
        task_id = task_id or f"task-{uuid.uuid4().hex[:16]}"
        lane_name = lane_name.strip() or ("主线" if track_type == "主线" else track_type)
        meta = {
            "type": "work-task",
            "task_id": task_id,
            "project_id": project_meta["project_id"],
            "project_name": project_meta["name"],
            "title": title.strip(),
            "aliases": sorted({str(item).strip() for item in (aliases or []) if str(item).strip()}),
            "status": status,
            "required": bool(required),
            "work_type": project_meta.get("work_type", "项目"),
            "work_domain": project_meta.get("work_domain", "项目管理"),
            "work_category": project_meta.get("work_category", "其他项目"),
            "track_type": track_type,
            "lane_name": lane_name,
            "parent_task_id": parent_task_id,
            "predecessor_task_ids": list(dict.fromkeys(predecessor_task_ids or [])),
            "latest_node_id": "",
            "derived_from_history": bool(derived_from_history),
            "source": source,
            "status_history": [],
            "created_at": now,
            "updated_at": now,
            "completed_at": now if status == "已完成" else "",
        }
        content = (
            f"---\n{_safe_yaml(meta)}\n---\n\n"
            f"# {project_meta['name']} · {title.strip()}\n\n"
            f"## 任务说明\n\n{description.strip() or '无'}\n\n"
            f"## 关联资料\n\n{related_path.strip() or '无'}\n"
        )
        task_path = self._task_path(project_meta["name"], title.strip())
        if task_path.exists():
            task_path = task_path.with_name(f"{task_path.stem}—{task_id[-6:]}{task_path.suffix}")
        _atomic_write(task_path, content)
        self._task_cache.pop(str(task_path), None)
        self._sync_project_status_from_tasks(project_meta["project_id"])
        self.sync_native_views(project_meta["project_id"])
        self.write_work_panorama_pages()
        return self.get_task(task_id) or meta

    @_timed_method
    def update_task(self, task_id: str, changes: dict[str, Any], *, sync_project: bool = True) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise WorklogError(f"任务不存在：{task_id}")
        path = Path(task.pop("path"))
        task.pop("hash", None)
        old_status = task.get("status", "待开始")
        new_status = changes.get("status", old_status)
        if new_status not in VALID_STATUSES:
            raise WorklogError(f"无效任务状态：{new_status}")
        allowed = {
            "title", "aliases", "status", "required", "track_type", "lane_name", "parent_task_id",
            "predecessor_task_ids", "latest_node_id", "description", "related_path",
        }
        description = changes.get("description", task.pop("description", ""))
        related_path = changes.get("related_path", task.pop("related_path", ""))
        for key, value in changes.items():
            if key in allowed and key not in {"description", "related_path"}:
                task[key] = value
        if new_status != old_status:
            history = list(task.get("status_history") or [])
            history.append({"from": old_status, "to": new_status, "at": _now().isoformat()})
            task["status_history"] = history
            task["completed_at"] = _now().isoformat() if new_status == "已完成" else ""
        task["updated_at"] = _now().isoformat()
        content = (
            f"---\n{_safe_yaml(task)}\n---\n\n"
            f"# {task['project_name']} · {task['title']}\n\n"
            f"## 任务说明\n\n{str(description).strip() or '无'}\n\n"
            f"## 关联资料\n\n{str(related_path).strip() or '无'}\n"
        )
        _atomic_write(path, content)
        self._task_cache.pop(str(path), None)
        if sync_project:
            self._sync_project_status_from_tasks(task["project_id"])
            self.sync_native_views(task["project_id"])
            self.write_work_panorama_pages()
        return self.get_task(task_id) or task

    def close_superseded_tasks(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Close unfinished tasks that have a later node on the same work lane.

        This repairs historical imports where a work node was correctly closed
        after a successor appeared, but its associated task was left open.
        The terminal task in each lane is deliberately left untouched.
        """
        nodes = self.list_nodes()
        by_task: dict[str, list[dict[str, Any]]] = {}
        by_lane: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for node in nodes:
            by_lane.setdefault((node["project_id"], node.get("lane_id", "main")), []).append(node)
            if node.get("task_id"):
                by_task.setdefault(node["task_id"], []).append(node)
        for lane_nodes in by_lane.values():
            lane_nodes.sort(key=lambda item: (
                item.get("start_time") or item.get("occurred_at", ""), item.get("node_id", "")
            ))

        candidates: list[dict[str, Any]] = []
        for task in self.list_tasks():
            if task.get("status") == "已完成":
                continue
            task_nodes = by_task.get(task.get("task_id", ""), [])
            if not task_nodes:
                continue
            latest_by_lane: dict[tuple[str, str], dict[str, Any]] = {}
            for node in task_nodes:
                key = (node["project_id"], node.get("lane_id", "main"))
                current = latest_by_lane.get(key)
                if not current or (node.get("start_time") or node.get("occurred_at", ""), node.get("node_id", "")) > (
                    current.get("start_time") or current.get("occurred_at", ""), current.get("node_id", "")
                ):
                    latest_by_lane[key] = node
            successors = []
            for key, task_latest in latest_by_lane.items():
                lane_nodes = by_lane.get(key, [])
                later = [node for node in lane_nodes if (
                    node.get("start_time") or node.get("occurred_at", ""), node.get("node_id", "")
                ) > (task_latest.get("start_time") or task_latest.get("occurred_at", ""), task_latest.get("node_id", ""))]
                if later:
                    successors.extend(later)
            if successors:
                candidates.append({
                    "task_id": task["task_id"],
                    "project_id": task["project_id"],
                    "project_name": task["project_name"],
                    "title": task["title"],
                    "old_status": task["status"],
                    "successor_node_ids": sorted({node["node_id"] for node in successors}),
                })

        if not dry_run:
            affected_projects = set()
            for item in candidates:
                self.update_task(item["task_id"], {"status": "已完成"}, sync_project=False)
                affected_projects.add(item["project_id"])
            for project_id in affected_projects:
                self._sync_project_status_from_tasks(project_id)
                self.sync_overview(project_id)
            if affected_projects:
                self.sync_native_views()
                self.write_work_panorama_pages()
        return {"updated": len(candidates), "tasks": candidates, "dry_run": dry_run}

    def close_superseded_nodes(self, *, dry_run: bool = False, project: str = "", track_type: str = "") -> dict[str, Any]:
        """Close every unfinished non-terminal node in its own work lane.

        A later node means the prior step has already handed over to the next
        step.  This is intentionally lane-scoped, so parallel branches keep
        their own terminal node and are never closed by activity on another
        branch.
        """
        lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        selected = self.get_project(project) if project else None
        if project and not selected:
            raise UnknownProjectError(f"项目不存在：{project}")
        for node in self.list_nodes(project_id=selected["project_id"] if selected else ""):
            if track_type and node.get("track_type") != track_type:
                continue
            lanes.setdefault((node["project_id"], node.get("lane_id", "main")), []).append(node)
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for lane_nodes in lanes.values():
            lane_nodes.sort(key=lambda item: (
                item.get("start_time") or item.get("occurred_at", ""), item.get("node_id", "")
            ))
            for index, node in enumerate(lane_nodes[:-1]):
                if node.get("status") != "已完成":
                    candidates.append((node, lane_nodes[index + 1]))

        result = {
            "updated": len(candidates),
            "nodes": [
                {
                    "node_id": node["node_id"], "project_name": node["project_name"],
                    "summary": node.get("summary", ""), "old_status": node.get("status", ""),
                    "successor_node_id": successor["node_id"],
                }
                for node, successor in candidates
            ],
            "dry_run": dry_run,
        }
        if dry_run:
            return result

        affected_projects = set()
        for node, successor in candidates:
            history = list(node.get("status_history") or [])
            history.append({
                "from": node.get("status", "进行中"), "to": "已完成", "at": _now().isoformat(),
                "reason": "同一工作线已有后续节点，按历史状态校正规则闭环",
            })
            self._rewrite_node_meta(node, {
                "status": "已完成",
                "end_time": node.get("end_time") or successor.get("start_time") or successor.get("occurred_at", ""),
                "status_history": history,
                "review_state": "已确认" if node.get("review_state") != "待确认" else "待确认",
            })
            affected_projects.add(node["project_id"])
        for project_id in affected_projects:
            self.sync_overview(project_id)
        if affected_projects:
            self.sync_native_views()
            self.write_work_panorama_pages()
        return result

    def _sync_project_status_from_tasks(self, project_id: str) -> None:
        self._warning_project_ids.add(project_id)
        project = self.get_project(project_id)
        if not project or project.get("project_mode") != "bounded" or project.get("project_status") == "暂停":
            return
        required = [item for item in self.list_tasks(project_id) if item.get("required")]
        if not required:
            return
        desired = "已完成" if all(item.get("status") == "已完成" for item in required) else "进行中"
        if project.get("project_status") != desired:
            self.update_project(project_id, {"project_status": desired}, sync_overview=False)

    def discover_overviews(self) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        patterns = ("项目管理/**/*项目总览*.md", "体系评审/**/*项目总览*.md")
        for pattern in patterns:
            for path in self.vault_root.glob(pattern):
                meta, body = _parse_frontmatter(path)
                heading = re.search(r"^#\s+(.+)$", body, re.M)
                name = str(meta.get("项目名称") or (heading.group(1) if heading else path.parent.name))
                name = re.sub(r"[-－]?项目总览$", "", name).strip()
                major = "其他" if "体系评审" in path.parts else "项目推进"
                key = "其他支持事项" if major == "其他" else "人才发展战略：构建系统性能力提升体系，赋能组织人才梯队建设"
                status_text = str(meta.get("状态") or "进行中")
                status = "已完成" if "完成" in status_text else ("阻塞" if "阻塞" in status_text else "进行中")
                relative = path.relative_to(self.vault_root).as_posix()
                project = self.create_project(
                    name=name,
                    major_work=major,
                    key_point=key,
                    status=status,
                    overview_path=relative,
                )
                if not project.get("overview_path"):
                    project = self.update_project(project["project_id"], {"overview_path": relative})
                created.append(project)
        return created

    def _render_node(self, meta: dict[str, Any], summary: str, next_action: str, related_path: str) -> str:
        lines = [
            "---",
            _safe_yaml(meta),
            "---",
            "",
            f"# {meta['project_name']} · {meta['kind']}",
            "",
            "## 本次进展",
            summary.strip(),
            "",
            "## 下一步动作",
            next_action.strip() or "无",
            "",
            "## 关联资料",
            related_path.strip() or "无",
            "",
        ]
        return "\n".join(lines)

    def _rewrite_node_meta(self, node: dict[str, Any], changes: dict[str, Any]) -> None:
        path = Path(node["path"])
        meta, body = _parse_frontmatter(path)
        meta.update(changes)
        _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
        self._refresh_node_cache_entry(path)

    def _latest_lane_node(self, project_id: str, lane_id: str) -> dict[str, Any] | None:
        return next(
            (
                item for item in self.list_nodes(project_id=project_id)
                if item.get("project_id") == project_id and item.get("lane_id", "main") == lane_id
            ),
            None,
        )

    def _close_predecessors(self, predecessor_ids: list[str], successor_time: str) -> None:
        for node_id in predecessor_ids:
            previous = self.get_node(node_id)
            if not previous or previous.get("status") == "已完成":
                continue
            history = list(previous.get("status_history") or [])
            history.append({
                "from": previous.get("status", "进行中"),
                "to": "已完成",
                "at": _now().isoformat(),
                "reason": "创建后续节点自动闭环",
            })
            self._rewrite_node_meta(previous, {
                "status": "已完成",
                "end_time": previous.get("end_time") or successor_time,
                "status_history": history,
            })

    @_timed_method
    def record_node(
        self,
        *,
        project: str,
        summary: str,
        status: str = "进行中",
        next_action: str = "",
        kind: str = "进展",
        source: str = "手工记录",
        occurred_at: str | datetime | None = None,
        time_precision: str = "minute",
        related_path: str = "",
        include_in_report: bool = True,
        source_ref: str = "",
        idempotency_key: str = "",
        supersedes: str = "",
        revision: int = 1,
        expected_source_hash: str = "",
        sync_overview: bool = True,
        check_source_ref: bool = True,
        lane_id: str = "",
        lane_name: str = "",
        track_type: str = "主线",
        relation_type: str = "顺序",
        predecessor_ids: Iterable[str] | None = None,
        parent_node_id: str = "",
        start_time: str | datetime | None = None,
        end_time: str | datetime | None = None,
        keep_open: bool = False,
        task_id: str = "",
        task_status: str = "",
        intent_type: str = "",
        review_flags: Iterable[str] | None = None,
        review_state: str = "已确认",
        excluded: bool = False,
        exclusion_reason: str = "",
    ) -> dict[str, Any]:
        if not summary.strip():
            raise WorklogError("工作进展不能为空")
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}。请先确认主要工作和关键点。")
        if status not in VALID_STATUSES:
            raise WorklogError(f"无效状态：{status}")
        if kind not in VALID_KINDS:
            raise WorklogError(f"无效类型：{kind}")
        if track_type not in VALID_TRACK_TYPES:
            raise WorklogError(f"无效工作线类型：{track_type}")
        if relation_type not in VALID_RELATION_TYPES:
            raise WorklogError(f"无效关系类型：{relation_type}")
        task = self.get_task(task_id) if task_id else None
        if task_id and not task:
            raise WorklogError(f"任务不存在：{task_id}")
        if task and task.get("project_id") != project_meta["project_id"]:
            raise WorklogError("任务与项目不匹配")
        if task_status and task_status not in VALID_STATUSES:
            raise WorklogError(f"无效任务状态：{task_status}")
        if intent_type and intent_type not in VALID_INTENT_TYPES:
            raise WorklogError(f"无效记录意图：{intent_type}")
        if isinstance(occurred_at, datetime):
            occurred = occurred_at.replace(microsecond=0)
        elif occurred_at:
            occurred = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
        else:
            occurred = _now()
        lane_name = lane_name.strip() or ("主线" if track_type == "主线" else track_type)
        lane_id = lane_id.strip() or (
            "main" if track_type == "主线"
            else f"lane-{hashlib.sha1((project_meta['project_id'] + lane_name).encode('utf-8')).hexdigest()[:10]}"
        )
        predecessors = list(dict.fromkeys(str(item).strip() for item in (predecessor_ids or []) if str(item).strip()))
        if not predecessors and not source_ref and not supersedes:
            latest_lane = self._latest_lane_node(project_meta["project_id"], lane_id)
            if latest_lane:
                predecessors = [latest_lane["node_id"]]
        for predecessor in predecessors:
            if not self.get_node(predecessor):
                raise WorklogError(f"前置节点不存在：{predecessor}")
        if len(predecessors) > 1:
            relation_type = "汇合"
        if relation_type == "汇合" and any(
            (self.get_node(item) or {}).get("status") != "已完成" for item in predecessors
        ):
            status = "待开始"

        def iso_time(value: str | datetime | None, fallback: datetime | None = None) -> str:
            if isinstance(value, datetime):
                return value.replace(microsecond=0).isoformat()
            if value:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0).isoformat()
            return fallback.isoformat() if fallback else ""

        start_iso = iso_time(start_time, occurred)
        end_iso = iso_time(end_time)
        if status == "已完成" and not end_iso:
            end_iso = occurred.isoformat()

        with self.mutation_lock():
            existing_nodes = self.list_nodes(effective_only=False) if (source_ref or idempotency_key) else []
            for existing in existing_nodes:
                if source_ref and check_source_ref and existing.get("source_ref") == source_ref:
                    return existing
                if idempotency_key and existing.get("idempotency_key") == idempotency_key:
                    expected = {
                        "project_id": project_meta["project_id"],
                        "task_id": task_id,
                        "summary": summary.strip(),
                        "status": status,
                    }
                    mismatched = [
                        key for key, value in expected.items()
                        if value and str(existing.get(key) or "") != str(value)
                    ]
                    if mismatched or existing.get("excluded"):
                        raise ConflictError(
                            "幂等键已用于不同的工作记录，请重新生成同步草稿"
                        )
                    replay = dict(existing)
                    replay["idempotent_replay"] = True
                    return replay
            if supersedes and expected_source_hash:
                previous = self.get_node(supersedes)
                if not previous or previous.get("hash") != expected_source_hash:
                    raise ConflictError("原节点已在 Obsidian 中发生变化，请重新加载后再编辑")
            node_id = (
                f"node-import-{hashlib.sha1(source_ref.encode('utf-8')).hexdigest()[:20]}"
                if source_ref
                else f"node-{occurred:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
            )
            created_at = _now().isoformat()
            flags = list(dict.fromkeys(str(item) for item in (review_flags or []) if str(item)))
            if status == "待确认" and "状态待确认" not in flags:
                flags.append("状态待确认")
            if flags:
                review_state = "待确认"
            meta = {
                "type": "weekly-node",
                "node_id": node_id,
                "project_id": project_meta["project_id"],
                "project_name": project_meta["name"],
                "major_work": project_meta["major_work"],
                "key_point": project_meta["key_point"],
                "work_domain": project_meta.get("work_domain", ""),
                "work_category": project_meta.get("work_category", ""),
                "work_type": project_meta.get("work_type", "项目"),
                "view_tags": project_meta.get("view_tags", []),
                "occurred_at": occurred.isoformat(),
                "time_precision": time_precision,
                "kind": kind,
                "status": status,
                "source": source if source in VALID_SOURCES else source,
                "include_in_report": bool(include_in_report),
                "supersedes": supersedes,
                "revision": revision,
                "source_ref": source_ref,
                "idempotency_key": idempotency_key,
                "lane_id": lane_id,
                "lane_name": lane_name,
                "track_type": track_type,
                "relation_type": relation_type,
                "predecessor_ids": predecessors,
                "parent_node_id": parent_node_id,
                "start_time": start_iso,
                "end_time": end_iso,
                "keep_open": bool(keep_open),
                "task_id": task_id,
                "intent_type": intent_type,
                "review_flags": flags,
                "review_state": review_state,
                "excluded": bool(excluded),
                "exclusion_reason": exclusion_reason,
                "created_at": created_at,
                "summary": summary.strip(),
                "next_action": next_action.strip(),
                "related_path": related_path.strip(),
            }
            self._close_predecessors(predecessors, start_iso)
            path = self._node_path(occurred, node_id, project_meta["name"], summary)
            with self.timed_stage("write_node"):
                _atomic_write(path, self._render_node(meta, summary, next_action, related_path))
            self._refresh_node_cache_entry(path)
            if task:
                # A backdated addition/revision must not displace newer work.
                effective = [item for item in self.list_nodes(project_id=project_meta["project_id"])
                             if item.get("task_id") == task_id]
                latest_task_node = max(effective, key=lambda item: (
                    item.get("occurred_at", ""), item.get("created_at", ""),
                    int(item.get("revision") or 0), item.get("node_id", ""),
                ))
                task_changes: dict[str, Any] = {"latest_node_id": latest_task_node["node_id"]}
                if task_status:
                    task_changes["status"] = task_status
                self.update_task(task_id, task_changes, sync_project=False)
                self._sync_project_status_from_tasks(project_meta["project_id"])
            if sync_overview:
                with self.timed_stage("sync_overview"):
                    self.sync_overview(project_meta["project_id"])
                with self.timed_stage("sync_native_views"):
                    self.sync_native_views(project_meta["project_id"])
                with self.timed_stage("write_work_panorama"):
                    self.write_work_panorama_pages()
            created = self.get_node(node_id) or meta
            created["idempotent_replay"] = False
            self._warning_project_ids.add(project_meta["project_id"])
            return created

    def _read_node(self, path: Path) -> dict[str, Any] | None:
        raw = path.read_bytes()
        meta, body = _parse_frontmatter_text(raw.decode("utf-8"))
        if meta.get("type") != "weekly-node":
            return None
        body_summary = _extract_section(body, "本次进展")
        meta["summary"] = body_summary or str(meta.get("summary") or "")
        next_action = _extract_section(body, "下一步动作")
        meta["next_action"] = "" if next_action == "无" else (next_action or str(meta.get("next_action") or ""))
        related = _extract_section(body, "关联资料")
        meta["related_path"] = "" if related == "无" else (related or str(meta.get("related_path") or ""))
        meta["include_in_report"] = _as_bool(meta.get("include_in_report", True))
        meta["keep_open"] = _as_bool(meta.get("keep_open", False))
        meta["excluded"] = _as_bool(meta.get("excluded", False))
        meta["predecessor_ids"] = meta.get("predecessor_ids") or []
        meta["review_flags"] = meta.get("review_flags") or []
        meta["lane_id"] = meta.get("lane_id") or "main"
        meta["lane_name"] = meta.get("lane_name") or "主线"
        meta["track_type"] = meta.get("track_type") or "主线"
        meta["relation_type"] = meta.get("relation_type") or "顺序"
        meta["start_time"] = meta.get("start_time") or meta.get("occurred_at", "")
        meta["review_state"] = meta.get("review_state") or ("待确认" if meta.get("review_flags") else "已确认")
        meta["task_id"] = meta.get("task_id") or ""
        meta["intent_type"] = meta.get("intent_type") or ""
        meta["view_tags"] = meta.get("view_tags") or []
        meta["path"] = str(path)
        meta["hash"] = hashlib.sha256(raw).hexdigest()
        return meta

    def list_nodes(self, *, effective_only: bool = True, project_id: str = "") -> list[dict[str, Any]]:
        with self.timed_stage("list_nodes"):
            if self._node_snapshot_depth and self._all_nodes_snapshot is not None:
                nodes = [dict(item) for item in self._all_nodes_snapshot]
            else:
                with self.timed_stage("node_snapshot_build"):
                    paths = sorted(self.nodes_dir.rglob("*.md"))
                    active_paths = {str(path) for path in paths}
                    persistent = self._load_node_index() if self.node_index_path else {}
                    active_index_keys: set[str] = set()
                    for cached_path in set(self._node_cache) - active_paths:
                        self._node_cache.pop(cached_path, None)
                    nodes = []
                    for path in paths:
                        stat = path.stat()
                        key = str(path)
                        index_key = self._node_index_key(path)
                        active_index_keys.add(index_key)
                        cached = self._node_cache.get(key)
                        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                            item = dict(cached[2])
                        elif (
                            index_key in persistent
                            and persistent[index_key].get("mtime_ns") == stat.st_mtime_ns
                            and persistent[index_key].get("ctime_ns") == stat.st_ctime_ns
                            and persistent[index_key].get("size") == stat.st_size
                            and isinstance(persistent[index_key].get("item"), dict)
                        ):
                            item = dict(persistent[index_key]["item"])
                            item["path"] = key
                            self._node_cache[key] = (stat.st_mtime_ns, stat.st_size, dict(item))
                        else:
                            item = self._read_node(path)
                            if item:
                                self._node_cache[key] = (stat.st_mtime_ns, stat.st_size, dict(item))
                                if self.node_index_path:
                                    persistent[index_key] = {
                                        "mtime_ns": stat.st_mtime_ns,
                                        "ctime_ns": stat.st_ctime_ns,
                                        "size": stat.st_size,
                                        "item": dict(item),
                                    }
                                    self._persistent_node_index_dirty = True
                        if item:
                            nodes.append(item)
                    if self.node_index_path:
                        stale = set(persistent) - active_index_keys
                        if stale:
                            for index_key in stale:
                                persistent.pop(index_key, None)
                            self._persistent_node_index_dirty = True
                    if self._node_snapshot_depth:
                        self._all_nodes_snapshot = [dict(item) for item in nodes]
            if project_id:
                nodes = [item for item in nodes if item.get("project_id") == project_id]
        if effective_only:
            superseded = {item.get("supersedes") for item in nodes if item.get("supersedes")}
            nodes = [item for item in nodes if item.get("node_id") not in superseded and not item.get("excluded")]
        return sorted(nodes, key=lambda item: (
            item.get("occurred_at", ""), item.get("created_at", ""),
            int(item.get("revision") or 0), item.get("node_id", ""),
        ), reverse=True)

    def merge_projects(self, canonical: str, duplicates: Iterable[str]) -> dict[str, Any]:
        canonical_project = self.get_project(canonical)
        if not canonical_project:
            raise UnknownProjectError(f"主项目不存在：{canonical}")
        duplicate_projects = [self.get_project(item) for item in duplicates]
        duplicate_projects = [item for item in duplicate_projects if item and item["project_id"] != canonical_project["project_id"]]
        if not duplicate_projects:
            return canonical_project

        aliases = set(canonical_project.get("aliases") or [])
        overview_path = canonical_project.get("overview_path") or ""
        for duplicate in duplicate_projects:
            aliases.add(duplicate["name"])
            aliases.update(duplicate.get("aliases") or [])
            overview_path = overview_path or duplicate.get("overview_path") or ""
        canonical_project = self.update_project(
            canonical_project["project_id"],
            {"aliases": sorted(aliases), "overview_path": overview_path},
            sync_overview=False,
        )

        duplicate_ids = {item["project_id"] for item in duplicate_projects}
        for node in self.list_nodes(effective_only=False):
            if node.get("project_id") not in duplicate_ids:
                continue
            path = Path(node["path"])
            meta, body = _parse_frontmatter(path)
            meta["project_id"] = canonical_project["project_id"]
            meta["project_name"] = canonical_project["name"]
            meta["major_work"] = canonical_project["major_work"]
            meta["key_point"] = canonical_project["key_point"]
            meta["work_domain"] = canonical_project.get("work_domain", "")
            meta["work_category"] = canonical_project.get("work_category", "")
            meta["work_type"] = canonical_project.get("work_type", "项目")
            meta["view_tags"] = canonical_project.get("view_tags", [])
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")

        for task in self.list_tasks():
            if task.get("project_id") not in duplicate_ids:
                continue
            path = Path(task["path"])
            meta, body = _parse_frontmatter(path)
            meta.update({
                "project_id": canonical_project["project_id"],
                "project_name": canonical_project["name"],
                "work_type": canonical_project.get("work_type", "项目"),
                "work_domain": canonical_project.get("work_domain", ""),
                "work_category": canonical_project.get("work_category", ""),
            })
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")

        for duplicate in duplicate_projects:
            path = Path(duplicate["path"])
            duplicate.pop("path", None)
            duplicate.pop("hash", None)
            duplicate["type"] = "weekly-project-merged"
            duplicate["merged_into"] = canonical_project["project_id"]
            _atomic_write(path, f"---\n{_safe_yaml(duplicate)}\n---\n\n# 已合并项目\n\n此项目名称已作为别名合并到 `{canonical_project['name']}`。\n")
        self._invalidate_node_cache()
        self._task_cache.clear()
        self.sync_overview(canonical_project["project_id"])
        self.sync_native_views()
        self.write_work_panorama_pages()
        return self.get_project(canonical_project["project_id"]) or canonical_project

    def merge_tasks(self, canonical_task: str, duplicates: Iterable[str]) -> dict[str, Any]:
        canonical = self.get_task(canonical_task)
        if not canonical:
            raise WorklogError(f"主任务不存在：{canonical_task}")
        duplicate_tasks = [self.get_task(item) for item in duplicates]
        duplicate_tasks = [item for item in duplicate_tasks if item and item["task_id"] != canonical["task_id"]]
        if not duplicate_tasks:
            return canonical
        if any(item["project_id"] != canonical["project_id"] for item in duplicate_tasks):
            raise WorklogError("合并任务必须属于同一个项目；请先合并项目")

        aliases = set(canonical.get("aliases") or [])
        duplicate_ids = {item["task_id"] for item in duplicate_tasks}
        for duplicate in duplicate_tasks:
            aliases.add(duplicate["title"])
            aliases.update(duplicate.get("aliases") or [])
        canonical = self.update_task(canonical["task_id"], {"aliases": sorted(aliases)}, sync_project=False)
        for node in self.list_nodes(effective_only=False):
            if node.get("task_id") not in duplicate_ids:
                continue
            path = Path(node["path"])
            meta, body = _parse_frontmatter(path)
            meta["task_id"] = canonical["task_id"]
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
        for duplicate in duplicate_tasks:
            path = Path(duplicate["path"])
            meta, body = _parse_frontmatter(path)
            meta.update({"type": "work-task-merged", "merged_into": canonical["task_id"]})
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n# 已合并任务\n\n此任务已合并到 `{canonical['title']}`。\n")
        self._invalidate_node_cache()
        self._task_cache.clear()
        self._sync_project_status_from_tasks(canonical["project_id"])
        self.sync_overview(canonical["project_id"])
        self.sync_native_views()
        self.write_work_panorama_pages()
        return self.get_task(canonical["task_id"]) or canonical

    def move_task(self, task_id: str, target_project: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise WorklogError(f"任务不存在：{task_id}")
        target = self.get_project(target_project)
        if not target:
            raise UnknownProjectError(f"目标项目未登记：{target_project}")
        source_project_id = task["project_id"]
        if source_project_id == target["project_id"]:
            return task
        same_title = self.get_task(task["title"], target["project_id"])
        if same_title:
            raise WorklogError(f"目标项目已有同名任务：{task['title']}，请先确认是否合并")

        task_path = Path(task["path"])
        task_meta, task_body = _parse_frontmatter(task_path)
        task_meta.update({
            "project_id": target["project_id"],
            "project_name": target["name"],
            "work_type": target.get("work_type", "项目"),
            "work_domain": target.get("work_domain", ""),
            "work_category": target.get("work_category", ""),
            "updated_at": _now().isoformat(),
        })
        new_task_path = self._task_path(target["name"], task["title"])
        if new_task_path.exists() and new_task_path != task_path:
            raise WorklogError(f"目标任务文件已存在：{new_task_path}")
        _atomic_write(new_task_path, f"---\n{_safe_yaml(task_meta)}\n---\n\n{task_body.lstrip()}")
        if new_task_path != task_path:
            task_path.unlink()

        moved_nodes = [
            node for node in self.list_nodes(effective_only=False)
            if node.get("task_id") == task_id
        ]
        moved_ids = {node["node_id"] for node in moved_nodes}
        for node in moved_nodes:
            old_path = Path(node["path"])
            meta, body = _parse_frontmatter(old_path)
            meta.update({
                "project_id": target["project_id"],
                "project_name": target["name"],
                "major_work": target["major_work"],
                "key_point": target["key_point"],
                "work_domain": target.get("work_domain", ""),
                "work_category": target.get("work_category", ""),
                "work_type": target.get("work_type", "项目"),
                "view_tags": target.get("view_tags", []),
                "predecessor_ids": [item for item in (meta.get("predecessor_ids") or []) if item in moved_ids],
            })
            occurred = datetime.fromisoformat(str(meta.get("occurred_at", "")).replace("Z", "+00:00")).replace(tzinfo=None)
            new_path = self._node_path(occurred, meta["node_id"], target["name"], node.get("summary", ""))
            if new_path.exists() and new_path != old_path:
                raise WorklogError(f"目标节点文件已存在：{new_path}")
            _atomic_write(new_path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
            if new_path != old_path:
                old_path.unlink()

        self._task_cache.clear()
        self._invalidate_node_cache()
        self._sync_project_status_from_tasks(source_project_id)
        self._sync_project_status_from_tasks(target["project_id"])
        self.sync_overview(source_project_id)
        self.sync_overview(target["project_id"])
        self.sync_native_views()
        self.write_work_panorama_pages()
        return self.get_task(task_id) or task_meta

    def complete_project(self, project: str) -> dict[str, Any]:
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        completed_tasks = []
        for task in self.list_tasks(project_meta["project_id"]):
            if task.get("status") != "已完成":
                self.update_task(task["task_id"], {"status": "已完成"}, sync_project=False)
                completed_tasks.append(task["title"])
        self.update_project(project_meta["project_id"], {"project_status": "已完成"}, sync_overview=False)
        self.sync_overview(project_meta["project_id"])
        self.sync_native_views()
        self.write_work_panorama_pages()
        return {"project": self.get_project(project_meta["project_id"]), "completed_tasks": completed_tasks}

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        if self.node_index_path or self._node_snapshot_depth:
            return next(
                (
                    item for item in self.list_nodes(effective_only=False)
                    if item.get("node_id") == node_id
                ),
                None,
            )
        for path in self.nodes_dir.rglob("*.md"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    head = handle.read(900)
            except (OSError, UnicodeDecodeError):
                continue
            if f"node_id: {node_id}" not in head:
                continue
            node = self._read_node(path)
            if node and node.get("node_id") == node_id:
                return node
        return None

    def _project_folder(self, project: dict[str, Any]) -> Path:
        overview = self._overview_file(project)
        if overview:
            return overview.parent
        return self.vault_root / "项目管理" / project["name"]

    @staticmethod
    def _safe_name(value: str, fallback: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]+', "-", value).strip(" .-")
        return cleaned[:100] or fallback

    def _experience_index_path(self, project: dict[str, Any]) -> Path:
        return self._project_folder(project) / "项目经验索引.md"

    def initialize_project_experience(self, project: str, source_root: str = "") -> dict[str, Any]:
        """Create readable project experience folders without moving source files."""
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        folder = self._project_folder(project_meta)
        overview = self._overview_file(project_meta)
        if overview and not overview.exists():
            _atomic_write(
                overview,
                "---\n" + _safe_yaml({"项目名称": project_meta["name"], "状态": project_meta.get("project_status", "进行中")}) + "\n---\n\n"
                f"# {project_meta['name']}\n\n## 项目背景\n\n由周报系统创建，后续可在此补充项目背景、流程和产出。\n",
            )
        directories = {
            "流程": folder / "流程",
            "问题与决策": folder / "问题与决策",
            "产出": folder / "产出",
            "复盘": folder / "复盘",
        }
        for item in directories.values():
            item.mkdir(parents=True, exist_ok=True)
        index = self._experience_index_path(project_meta)
        if not index.exists():
            source_line = f"- 原始资料根目录：`{source_root}`\n" if source_root else "- 原始资料根目录：待补充\n"
            _atomic_write(index, "\n".join([
                "---",
                _safe_yaml({
                    "type": "project-experience-index",
                    "project_id": project_meta["project_id"],
                    "project_name": project_meta["name"],
                    "source_root": source_root,
                    "created_at": _now().isoformat(),
                }),
                "---", "", f"# {project_meta['name']} · 项目经验索引", "",
                "## 使用说明", "",
                "项目流程记录事实与步骤；问题与决策记录异常、选择和验证结果；复盘提炼项目结论；只有已确认的方法进入方法论库。", "",
                "## 来源", "", source_line.rstrip(), "",
                "## 导航", "",
                "- [[流程|流程]]",
                "- [[问题与决策|问题与决策]]",
                "- [[产出|产出]]",
                "- [[复盘|复盘]]", "",
            ]))
        return {
            "project": self._public_record(project_meta),
            "folder": str(folder),
            "index": str(index),
            "directories": {key: str(value) for key, value in directories.items()},
        }

    @staticmethod
    def _format_size(size: int) -> str:
        units = ("B", "KB", "MB", "GB", "TB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{size} B"

    @staticmethod
    def _ignore_material(path: Path) -> bool:
        """Ignore operating-system metadata and temporary Office files."""
        name = path.name
        return (
            name == ".DS_Store" or name == "Thumbs.db" or name.startswith("._")
            or name.startswith("~$")
        )

    @classmethod
    def _classify_material(cls, item: Path, relative: Path) -> tuple[str, str, bool]:
        """Return one authoritative destination, display group and review flag.

        Existing projects may use the original fixed directory convention, while
        training-week projects commonly use date directories.  Directory rules
        therefore take precedence when they are explicit; otherwise file name,
        path semantics and suffix determine the destination.
        """
        parts = relative.parts
        if not parts:
            return "培训通知与海报", "待确认项目资料 / 根目录", True
        top = parts[0]
        period = top if len(parts) > 1 else "根目录"
        path_text = relative.as_posix().lower()
        name_text = item.name.lower()
        suffix = item.suffix.lower()

        if suffix == ".md" and "发布包" in parts and "视频发布资料" in name_text:
            return "课程资料与视频", f"课程资料与视频 / {period}", False

        if top == "temp_convert":
            return "课程资料与视频", "辅助转换材料（不作正式依据）", False
        if suffix == ".md" and "研讨" in name_text and "截取视频" in name_text:
            return "沟通与会议纪要", "研讨转写（未经校对，不作实体依据）", False
        if (suffix == ".md" and "解读培训" in name_text) or (suffix == ".zip" and item.stem == "培训" and "培训" in path_text):
            return "课程资料与视频", f"课程资料与视频 / {period}", False

        if top == "培训视频&课件":
            label = parts[1] if len(parts) > 1 else "未分期"
            return "课程资料与视频", f"课程资料与视频 / {label}", False
        if top == "培训数据":
            label = parts[1] if len(parts) > 1 else "汇总"
            if "培训总结输出" in parts or "报告" in item.name or "简报" in item.name:
                return "培训总结报告与宣传", f"培训总结报告与宣传 / {label}", False
            return "培训参与数据汇总", f"培训参与数据汇总 / {label}", False
        if "海报" in top or "二维码" in top:
            return "培训通知与海报", f"培训通知与海报 / {top}", False

        if "会议纪要" in name_text:
            return "沟通与会议纪要", f"沟通与会议纪要 / {period}", False

        if "月报" in name_text:
            if "数据包" in name_text or "运营数据" in name_text:
                return "培训参与数据汇总", f"培训参与数据汇总 / {period}", False
            if "个人工作事项" in name_text or "项目材料" in name_text:
                return "培训总结报告与宣传", f"培训总结报告与宣传 / {period}", False

        summary_words = ("总结报告", "培训总结", "数据简报", "总结宣传")
        if "总结报告" in parts or any(word in path_text for word in summary_words):
            return "培训总结报告与宣传", f"培训总结报告与宣传 / {period}", False

        data_words = (
            "参训统计", "参训情况", "参与统计", "参与情况", "直播明细", "观看明细",
            "人员情况明细", "人员基表", "统计工具", "自动统计", "统计规则", "数据汇总", "数据统计", "固定名单版",
        )
        if suffix in {".json", ".csv", ".xls", ".xlsx"} or any(word in path_text for word in data_words):
            return "培训参与数据汇总", f"培训参与数据汇总 / {period}", False
        if suffix == ".html" and item.is_file():
            with item.open(encoding="utf-8", errors="replace") as handle:
                header = handle.read(32768)
            if "培训参训情况" in header and "直播明细" in header and "参与率" in header:
                return "培训参与数据汇总", f"培训参与数据汇总 / {period}", False

        notice_words = ("海报", "banner", "二维码", "宣传图", "通知图")
        if suffix == ".eml":
            destination = "培训通知与海报" if "通知" in name_text else "沟通与会议纪要"
            return destination, f"{destination} / {period}", False
        if any(word in path_text for word in notice_words):
            return "培训通知与海报", f"培训通知与海报 / {period}", False

        if "课程简介" in name_text or "发布素材" in parts or suffix in {
            ".mp4", ".mov", ".avi", ".mkv", ".ppt", ".pptx", ".pdf", ".docx", ".doc", ".wps", ".mp3", ".wav",
        }:
            return "课程资料与视频", f"课程资料与视频 / {period}", False

        if suffix in {".png", ".jpg", ".jpeg"}:
            if any(item.with_suffix(candidate).exists() for candidate in (".pptx", ".ppt", ".pdf")):
                return "课程资料与视频", f"课程资料与视频 / {period}", False
            if "课程封面" in name_text:
                return "课程资料与视频", f"课程资料与视频 / {period}", False
            return "培训通知与海报", f"培训通知与海报 / {period}", False

        if suffix == ".html" and "报告" in name_text:
            return "培训总结报告与宣传", f"培训总结报告与宣传 / {period}", False

        return "培训通知与海报", f"待确认项目资料 / {period}", True

    @classmethod
    def _material_group(cls, relative: Path, item: Path | None = None) -> str:
        source = item or relative
        return cls._classify_material(source, relative)[1]

    @staticmethod
    def _material_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".md" and "月报" in path.name:
            return "月报材料"
        if suffix == ".md" and "解读培训" in path.name:
            return "培训讲稿"
        if suffix == ".md" and "研讨" in path.name and "截取视频" in path.name:
            return "研讨转写（未经校对）"
        labels = {
            ".mp4": "视频", ".mov": "视频", ".avi": "视频", ".mkv": "视频",
            ".pptx": "课件", ".ppt": "课件", ".pdf": "课件/文档", ".docx": "文档",
            ".doc": "文档", ".wps": "文档", ".eml": "邮件", ".txt": "文本资料",
            ".mp3": "音频", ".wav": "音频", ".m4a": "音频", ".py": "辅助脚本",
            ".xlsx": "数据表", ".xls": "数据表", ".csv": "数据表",
            ".json": "统计规则配置",
            ".md": "会议纪要", ".html": "网页资料/报告",
            ".png": "图片", ".jpg": "图片", ".jpeg": "图片",
            ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包",
        }
        return labels.get(suffix, suffix.lstrip(".").upper() or "其他")

    @staticmethod
    def _file_uri(path: Path) -> str:
        return path.resolve().as_uri()

    def _material_file_link(self, path: Path) -> str:
        label = _clean_cell(path.name).replace("[", "\\[").replace("]", "\\]")
        return f"[{label}]({self._file_uri(path)})"

    def _material_destination(self, group: str, item: Path, relative: Path) -> str:
        return self._classify_material(item, relative)[0]

    def _material_inventory(self, source: Path, *, project_name: str = "") -> tuple[
        list[tuple[Path, Path, int]], list[str], dict[str, str]
    ]:
        """Collect indexable files and collapse byte-identical repeated copies."""
        candidates: list[tuple[Path, Path, int]] = []
        ignored: list[str] = []
        for item in sorted(source.rglob("*"), key=lambda path: str(path).lower()):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            if (project_name == "AI生产力培训周第二阶段"
                    and relative.parts[0] == "AI 生产力提速培训周第三期"):
                continue
            if self._ignore_material(item):
                ignored.append(relative.as_posix())
                continue
            try:
                candidates.append((item, relative, item.stat().st_size))
            except OSError:
                continue

        duplicate_of: dict[str, str] = {}
        retained: list[tuple[Path, Path, int]] = []
        by_name_size: dict[tuple[str, int], list[tuple[Path, Path, int]]] = defaultdict(list)
        for entry in candidates:
            by_name_size[(entry[0].name.lower(), entry[2])].append(entry)
        for entries in by_name_size.values():
            if len(entries) == 1:
                retained.extend(entries)
                continue
            by_hash: dict[str, list[tuple[Path, Path, int]]] = defaultdict(list)
            for entry in entries:
                by_hash[_file_hash(entry[0])].append(entry)
            for identical in by_hash.values():
                retained.append(identical[0])
                kept = identical[0][1].as_posix()
                for duplicate in identical[1:]:
                    duplicate_of[duplicate[1].as_posix()] = kept
        retained.sort(key=lambda entry: str(entry[1]).lower())
        return retained, sorted(ignored), dict(sorted(duplicate_of.items()))

    def _material_page_paths(self, project: dict[str, Any]) -> dict[str, Path]:
        folder = self._project_folder(project)
        return {
            "课程资料与视频": folder / "产出" / "课程资料与视频.md",
            "培训参与数据汇总": folder / "产出" / "培训参与数据汇总.md",
            "培训通知与海报": folder / "产出" / "培训通知与海报.md",
            "培训总结报告与宣传": folder / "产出" / "培训总结报告与宣传.md",
            "沟通与会议纪要": folder / "流程" / "沟通与会议纪要.md",
            "其他资料": folder / "其他资料.md",
        }

    def _existing_material_groups(self, project: dict[str, Any]) -> dict[str, str]:
        """Preserve manually refined group headings for already indexed paths."""
        groups: dict[str, str] = {}
        for page in self._material_page_paths(project).values():
            if not page.exists():
                continue
            text = page.read_text(encoding="utf-8")
            managed = re.search(
                re.escape(MATERIAL_START) + r"(.*?)" + re.escape(MATERIAL_END),
                text,
                re.S,
            )
            if not managed:
                continue
            heading = ""
            for line in managed.group(1).splitlines():
                if line.startswith("### "):
                    heading = line[4:].strip()
                    continue
                row = re.search(r"\|\s*`([^`]+)`\s*\|\s*$", line)
                if heading and row:
                    groups[row.group(1)] = heading
        return groups

    def build_material_index(self, project: str, source_root: str) -> dict[str, Any]:
        """Write source-file metadata into the matching project output pages."""
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        source = Path(source_root)
        if not source.exists() or not source.is_dir():
            raise WorklogError(f"原始资料目录不存在：{source_root}")
        self.initialize_project_experience(project_meta["project_id"], str(source))
        inventory, ignored, source_duplicates = self._material_inventory(source, project_name=project_meta["name"])
        existing_groups = self._existing_material_groups(project_meta)
        groups: dict[str, list[tuple[Path, Path, int]]] = {}
        total_size = 0
        for item, relative, size in inventory:
            groups.setdefault(self._material_group(relative, item), []).append((item, relative, size))
            total_size += size
        destinations: dict[str, list[tuple[Path, Path, int]]] = {
            "课程资料与视频": [], "培训参与数据汇总": [], "培训通知与海报": [],
            "培训总结报告与宣传": [], "沟通与会议纪要": [], "其他资料": [],
        }
        for group, items in groups.items():
            for item, relative, size in items:
                target = self._material_destination(group, item, relative)
                destinations[target].append((item, relative, size))

        def render_section(title: str, items: list[tuple[Path, Path, int]]) -> str:
            by_group: dict[str, list[tuple[Path, Path, int]]] = {}
            for item, relative, size in items:
                group = existing_groups.get(relative.as_posix(), self._material_group(relative, item))
                if group.startswith("待确认项目资料"):
                    group = self._material_group(relative, item)
                by_group.setdefault(group, []).append((item, relative, size))
            lines = [MATERIAL_START, "## 已索引原始资料", "", f"- 来源目录：`{source}`", f"- 本页索引：{len(items)} 个文件。", "- 文件保留在来源目录；这里可直接按文件名、类型和路径搜索。", ""]
            for group, grouped_items in sorted(by_group.items()):
                lines.extend([f"### {group}", "", "| 文件 | 类型 | 大小 | 原始路径 |", "|---|---|---:|---|"])
                for item, relative, size in sorted(grouped_items, key=lambda value: str(value[1]).lower()):
                    lines.append(f"| {self._material_file_link(item)} | {self._material_type(item)} | {self._format_size(size)} | `{relative.as_posix()}` |")
                lines.append("")
            lines.append(MATERIAL_END)
            return "\n".join(lines)

        page_paths = self._material_page_paths(project_meta)
        for title, path in page_paths.items():
            if path.exists():
                text = path.read_text(encoding="utf-8")
            else:
                text = f"# {title}\n\n由项目资料索引自动维护。\n"
            section = render_section(title, destinations[title])
            pattern = re.compile(re.escape(MATERIAL_START) + r".*?" + re.escape(MATERIAL_END), re.S)
            updated = pattern.sub(lambda _match: section, text) if pattern.search(text) else text.rstrip() + "\n\n" + section + "\n"
            _atomic_write(path, updated)
        audit = self.audit_material_index(project_meta["project_id"], str(source), write_report=True)
        return {
            "project": self._public_record(project_meta),
            "pages": {key: str(value) for key, value in page_paths.items()},
            "file_count": sum(len(items) for items in groups.values()), "group_count": len(groups),
            "ignored": ignored, "source_duplicates": source_duplicates,
            "total_size": total_size, "audit": audit,
        }

    def audit_material_index(self, project: str, source_root: str, *, write_report: bool = True) -> dict[str, Any]:
        """Detect missing, duplicate, stale, or uncertain material-index entries."""
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        source = Path(source_root)
        if not source.exists() or not source.is_dir():
            raise WorklogError(f"原始资料目录不存在：{source_root}")
        expected: dict[str, set[str]] = {key: set() for key in self._material_page_paths(project_meta)}
        unknown_types: list[str] = []
        suspicious: list[str] = []
        inventory, ignored, source_duplicates = self._material_inventory(source, project_name=project_meta["name"])
        for item, relative, _size in inventory:
            group = self._material_group(relative, item)
            target = self._material_destination(group, item, relative)
            relative_text = relative.as_posix()
            expected[target].add(relative_text)
            if self._material_type(item) in {"其他", item.suffix.lstrip(".").upper()}:
                unknown_types.append(relative_text)
            if self._classify_material(item, relative)[2] or group.endswith("未分期"):
                suspicious.append(relative_text)

        missing: dict[str, list[str]] = {}
        extra: dict[str, list[str]] = {}
        duplicate_locations: dict[str, list[str]] = {}
        observed_by_path: dict[str, list[str]] = {}
        for title, page in self._material_page_paths(project_meta).items():
            text = page.read_text(encoding="utf-8") if page.exists() else ""
            managed = re.search(re.escape(MATERIAL_START) + r"(.*?)" + re.escape(MATERIAL_END), text, re.S)
            rows = re.findall(r"\|[^\n]*\|\s*`([^`]+)`\s*\|", managed.group(1) if managed else "")
            actual = set(rows)
            for path in actual:
                observed_by_path.setdefault(path, []).append(title)
            absent = sorted(expected[title] - actual)
            unexpected = sorted(actual - expected[title])
            if absent:
                missing[title] = absent
            if unexpected:
                extra[title] = unexpected
        for path, locations in observed_by_path.items():
            if len(locations) > 1:
                duplicate_locations[path] = locations

        ok = not any((missing, extra, duplicate_locations, unknown_types, suspicious))
        result = {
            "ok": ok, "project": self._public_record(project_meta), "source_root": str(source),
            "expected_file_count": sum(len(items) for items in expected.values()),
            "missing": missing, "extra": extra, "duplicates": duplicate_locations,
            "ignored": ignored, "source_duplicates": source_duplicates,
            "unknown_types": sorted(unknown_types), "suspicious": sorted(suspicious),
        }
        if write_report:
            report = self._project_folder(project_meta) / "产出" / "资料索引核查.md"
            def render_items(values: Iterable[str], empty: str = "无") -> list[str]:
                items = list(values)
                return [f"- `{item}`" for item in items] if items else [f"- {empty}"]
            lines = [
                "---", _safe_yaml({
                    "type": "project-material-audit", "project_id": project_meta["project_id"],
                    "project_name": project_meta["name"], "source_root": str(source),
                    "checked_at": _now().isoformat(), "status": "通过" if ok else "待核查",
                }), "---", "", f"# {project_meta['name']} · 资料索引核查", "",
                f"- **核查结果**：{'通过' if ok else '发现待核查项'}",
                f"- **原始文件数**：{result['expected_file_count']}",
                "- **检查范围**：漏录、重复录入、路径失效/额外记录、未识别类型、疑似错误分类。", "",
                "## 漏录", "",
                *( [f"- {page}：`{path}`" for page, paths in missing.items() for path in paths] or ["- 无"] ), "",
                "## 重复或额外记录", "",
                *( [f"- {page}：`{path}`" for page, paths in extra.items() for path in paths] + [f"- `{path}` 出现在：{'、'.join(locations)}" for path, locations in duplicate_locations.items()] or ["- 无"] ), "",
                "## 未识别文件类型", "", *render_items(sorted(unknown_types)), "",
                "## 已忽略系统文件", "", *render_items(ignored), "",
                "## 已合并的重复源文件", "",
                *render_items([f"{path} -> {kept}" for path, kept in source_duplicates.items()]), "",
                "## 疑似分类错误", "", *render_items(sorted(suspicious)), "",
                "## 处理规则", "", "- 有待核查项时，不要静默修改业务结论；先核对原始文件与目标产出页，再刷新索引。", "",
            ]
            _atomic_write(report, "\n".join(lines))
            result["report_path"] = str(report)
        return result

    def _read_experience_card(self, path: Path) -> dict[str, Any] | None:
        try:
            meta, body = _parse_frontmatter(path)
        except (OSError, UnicodeDecodeError):
            return None
        if meta.get("type") not in {"project-issue", "method-candidate", "reusable-method"}:
            return None
        meta["path"] = str(path)
        meta["hash"] = _file_hash(path)
        meta["body"] = body.strip()
        if meta.get("type") in {"method-candidate", "reusable-method"}:
            for key, heading in {"summary": "核心做法", "steps": "操作步骤", "anti_pattern": "不适用情形"}.items():
                meta[key] = _extract_section(body, heading)
        return meta

    def list_issues(self, project: str = "", statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        project_meta = self.get_project(project) if project else None
        roots = [self._project_folder(project_meta) / "问题与决策"] if project_meta else list(self.vault_root.glob("项目管理/**/问题与决策"))
        wanted = set(statuses or [])
        cards: list[dict[str, Any]] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.glob("*.md"):
                card = self._read_experience_card(path)
                if not card or card.get("type") != "project-issue":
                    continue
                if wanted and card.get("status") not in wanted:
                    continue
                cards.append(card)
        return sorted(cards, key=lambda item: (item.get("updated_at", ""), item.get("created_at", "")), reverse=True)

    def record_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_ref = str(payload.get("project_id") or payload.get("project") or "").strip()
        project = self.get_project(project_ref)
        if not project:
            raise UnknownProjectError(f"项目未登记：{project_ref}。请先确认归属。")
        title = str(payload.get("title") or "").strip()
        problem = str(payload.get("problem") or "").strip()
        if not title or not problem:
            raise WorklogError("问题记录必须包含title和problem")
        status = str(payload.get("status") or "待分析")
        if status not in VALID_ISSUE_STATUSES:
            raise WorklogError(f"无效问题状态：{status}")
        self.initialize_project_experience(project["project_id"], str(payload.get("source_root") or ""))
        issue_id = str(payload.get("issue_id") or f"issue-{uuid.uuid4().hex[:12]}")
        issue_dir = self._project_folder(project) / "问题与决策"
        path = issue_dir / f"{issue_id}-{self._safe_name(title, '问题')}.md"
        if path.exists():
            raise WorklogError(f"问题卡已存在：{issue_id}")
        created = _now().isoformat()
        meta = {
            "type": "project-issue", "issue_id": issue_id,
            "project_id": project["project_id"], "project_name": project["name"],
            "stage": str(payload.get("stage") or "未分类"), "status": status,
            "source_node_ids": list(payload.get("source_node_ids") or []),
            "evidence_paths": list(payload.get("evidence_paths") or []),
            "created_at": created, "updated_at": created,
        }
        body = "\n".join([
            f"# {title}", "", "## 触发问题", "", problem, "",
            "## 决策与处理", "", str(payload.get("decision") or "待分析"), "",
            "## 处理结果", "", str(payload.get("result") or "待验证"), "",
            "## 下次注意", "", str(payload.get("reusable_note") or "待补充"), "",
            "## 关联证据", "",
            *( [f"- `{item}`" for item in meta["evidence_paths"]] or ["- 待补充"] ), "",
        ])
        _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body}")
        self.sync_overview(project["project_id"])
        return self._read_experience_card(path) or meta

    def resolve_issue(self, issue_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        issue = next((item for item in self.list_issues() if item.get("issue_id") == issue_id), None)
        if not issue:
            raise WorklogError(f"问题卡不存在：{issue_id}")
        status = str(changes.get("status") or issue.get("status") or "待分析")
        if status not in VALID_ISSUE_STATUSES:
            raise WorklogError(f"无效问题状态：{status}")
        path = Path(issue["path"])
        meta, body = _parse_frontmatter(path)
        title_match = re.search(r"^#\s+(.+)$", body, re.M)
        title = title_match.group(1).strip() if title_match else path.stem
        problem = _extract_section(body, "触发问题")
        decision = str(changes.get("decision") or _extract_section(body, "决策与处理") or "待分析")
        result = str(changes.get("result") or _extract_section(body, "处理结果") or "待验证")
        reusable_note = str(changes.get("reusable_note") or _extract_section(body, "下次注意") or "待补充")
        evidence_paths = list(changes.get("evidence_paths") or meta.get("evidence_paths") or [])
        meta.update({
            "status": status, "updated_at": _now().isoformat(), "evidence_paths": evidence_paths,
            "source_node_ids": list(changes.get("source_node_ids") or meta.get("source_node_ids") or []),
        })
        rewritten = "\n".join([
            f"# {title}", "", "## 触发问题", "", problem, "", "## 决策与处理", "", decision,
            "", "## 处理结果", "", result, "", "## 下次注意", "", reusable_note,
            "", "## 关联证据", "", *( [f"- `{item}`" for item in evidence_paths] or ["- 待补充"] ), "",
        ])
        _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{rewritten}")
        self.sync_overview(meta["project_id"])
        return self._read_experience_card(path) or meta

    def list_methods(self, *, status: str = "正式", query: str = "") -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for path in self.method_dir.rglob("*.md"):
            card = self._read_experience_card(path)
            if not card or card.get("type") != "reusable-method":
                continue
            if status and card.get("status") != status:
                continue
            text = " ".join([path.stem, card.get("summary", ""), card.get("applicable_scenarios", ""), card.get("body", "")])
            if query and _normalize(query) not in _normalize(text):
                continue
            cards.append(card)
        return sorted(cards, key=lambda item: item.get("confirmed_at", item.get("created_at", "")), reverse=True)

    def propose_method(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_ref = str(payload.get("project_id") or payload.get("project") or "").strip()
        project = self.get_project(project_ref)
        if not project:
            raise UnknownProjectError(f"项目未登记：{project_ref}")
        title = str(payload.get("title") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if not title or not summary:
            raise WorklogError("方法论候选必须包含title和summary")
        self.initialize_project_experience(project["project_id"])
        candidate_id = str(payload.get("candidate_id") or f"method-{uuid.uuid4().hex[:12]}")
        if any(c.get("candidate_id") == candidate_id for c in self.list_candidates()):
            raise ConflictError("候选ID已存在，不允许覆盖")
        candidate_dir = self._project_folder(project) / "复盘" / "方法论候选"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        path = candidate_dir / f"{candidate_id}-{self._safe_name(title, '方法论候选')}.md"
        created = _now().isoformat()
        meta = {
            "type": "method-candidate", "candidate_id": candidate_id, "status": "候选",
            "project_id": project["project_id"], "project_name": project["name"],
            "source_issue_ids": list(payload.get("source_issue_ids") or []),
            "evidence_paths": list(payload.get("evidence_paths") or []),
            "applicable_scenarios": str(payload.get("applicable_scenarios") or ""),
            "created_at": created, "updated_at": created,
        }
        body = "\n".join([
            f"# {title}", "", "## 核心做法", "", summary, "",
            "## 适用条件", "", meta["applicable_scenarios"] or "待确认", "",
            "## 操作步骤", "", str(payload.get("steps") or "待补充"), "",
            "## 不适用情形", "", str(payload.get("anti_pattern") or "待补充"), "",
            "> 这是候选做法，需经用户确认后才进入正式方法论库。", "",
        ])
        _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body}")
        return self._read_experience_card(path) or meta

    def list_candidates(self, project: str = "") -> list[dict[str, Any]]:
        projects = [self.get_project(project)] if project else self.list_projects()
        if project and not projects[0]:
            raise UnknownProjectError(f"项目未登记：{project}")
        cards, seen = [], set()
        for meta in projects:
            root = self._project_folder(meta) / "复盘" / "方法论候选"
            if not root.resolve().is_relative_to(self.vault_root.resolve()) or self.backups_dir.resolve() in root.resolve().parents:
                continue
            for path in sorted(root.glob("*.md")):
                if path.is_symlink() or path.resolve() in seen:
                    continue
                seen.add(path.resolve())
                card = self._read_experience_card(path)
                if card and card.get("type") == "method-candidate":
                    if card.get("project_id") != meta["project_id"]:
                        raise WorklogError(f"候选项目归属不匹配：{path.name}")
                    cards.append(card)
        return sorted(cards, key=lambda c: c.get("created_at", ""), reverse=True)

    def _method_index_text(self) -> tuple[Path, str]:
        path = self.method_dir / "00-方法论库索引.md"
        text = path.read_text(encoding="utf-8") if path.exists() else "# 方法论库索引\n"
        start, end = "<!-- reusable-method:start -->", "<!-- reusable-method:end -->"
        if text.count(start) != text.count(end) or text.count(start) > 1 or (start in text and text.index(start) > text.index(end)):
            raise WorklogError("方法索引受控区标记异常，请先核对；未覆盖索引")
        return path, text

    def refresh_method_index(self) -> dict[str, Any]:
        with self.mutation_lock():
            path, text = self._method_index_text()
            start, end = "<!-- reusable-method:start -->", "<!-- reusable-method:end -->"
            def cell(value):
                return re.sub(r"\s+", " ", str(value)).replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;")[:180]
            methods = self.list_methods()
            lines = [start, "## 正式方法卡（自动维护）", "", "| 方法 | 来源项目 | 适用条件 | 核心做法 |", "|---|---|---|---|"]
            for card in methods:
                title = re.search(r"^#\s+(.+)$", card["body"], re.M)
                title = title.group(1) if title else Path(card["path"]).stem
                link = Path(card["path"]).relative_to(self.vault_root).with_suffix("").as_posix()
                lines.append(f"| [[{link}|{cell(title)}]] | {cell(card.get('project_name', ''))} | {cell(card.get('applicable_scenarios') or _extract_section(card['body'], '适用条件'))} | {cell(_extract_section(card['body'], '核心做法'))} |")
            lines.extend(["", end])
            block = "\n".join(lines)
            updated = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, text, flags=re.S) if start in text else text.rstrip() + "\n\n" + block + "\n"
            if updated != text:
                _atomic_write(path, updated)
            return {"path": str(path), "count": len(methods), "changed": updated != text}

    def confirm_method(self, candidate_id: str, expected_hash: str = "") -> dict[str, Any]:
        with self.mutation_lock():
            matches = [c for c in self.list_candidates() if c.get("candidate_id") == candidate_id]
            if len(matches) != 1:
                raise WorklogError(f"候选不存在或ID不唯一：{candidate_id}")
            candidate = matches[0]
            existing = [m for m in self.list_methods(status="") if m.get("source_candidate_id") == candidate_id]
            if len(existing) > 1:
                raise WorklogError("同一候选存在多个正式方法，请先核对")
            if existing and candidate.get("status") == "已确认" and candidate.get("method_id") == existing[0].get("method_id"):
                self.refresh_method_index()
                return {**existing[0], "idempotent_replay": True}
            if expected_hash and candidate["hash"] != expected_hash:
                raise ConflictError("候选内容已变化，请重新审阅")
            if candidate.get("status") != "候选":
                raise WorklogError("候选状态与正式方法不一致，请核对")
            self._method_index_text()
            if existing:
                # Resume only a matching first write after interruption.
                method = existing[0]
                if method.get("source_candidate_hash") != candidate["hash"]:
                    raise ConflictError("候选与已有正式方法来源不一致")
            else:
                method_id = f"method-{uuid.uuid4().hex[:12]}"
                title = Path(candidate["path"]).stem.split("-", 2)[-1]
                destination = self.method_dir / f"{method_id}-{self._safe_name(title, '方法论')}.md"
                meta = {k: v for k, v in candidate.items() if k not in {"path", "hash", "body", "candidate_id", "summary", "steps", "anti_pattern"}}
                meta.update(type="reusable-method", method_id=method_id, status="正式", confirmed_at=_now().isoformat(), source_candidate_id=candidate_id, source_candidate_hash=candidate["hash"])
                body = candidate['body'].replace('> 这是候选做法，需经用户确认后才进入正式方法论库。', '')
                _atomic_write(destination, f"---\n{_safe_yaml(meta)}\n---\n\n{body}\n")
                method = self._read_experience_card(destination)
            old_meta, old_body = _parse_frontmatter(Path(candidate["path"]))
            old_meta.update(status="已确认", method_id=method["method_id"], confirmed_at=method["confirmed_at"])
            _atomic_write(Path(candidate["path"]), f"---\n{_safe_yaml(old_meta)}\n---\n\n{old_body.lstrip()}")
            self.refresh_method_index()
            return method

    def revise_method(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.mutation_lock():
            matches = [m for m in self.list_methods(status="") if m.get("method_id") == payload.get("method_id")]
            if len(matches) != 1:
                raise WorklogError("正式方法不存在或ID不唯一")
            method = matches[0]
            if not payload.get("expected_hash") or payload['expected_hash'] != method['hash']:
                raise ConflictError("方法内容已变化，请重新读取并审阅")
            changes = payload.get("changes")
            allowed = {"summary": "核心做法", "applicable_scenarios": "适用条件", "steps": "操作步骤", "anti_pattern": "不适用情形"}
            if not str(payload.get("reason") or "").strip() or not isinstance(changes, dict) or not changes or set(changes) - allowed.keys():
                raise WorklogError("须提供修订原因及支持的字段：summary/applicable_scenarios/steps/anti_pattern")
            if any(not isinstance(v, str) or not v.strip() for v in changes.values()):
                raise WorklogError("修订内容须为非空字符串")
            path = Path(method['path'])
            meta, body = _parse_frontmatter(path)
            for key, value in changes.items():
                heading = allowed[key]
                pattern = r"(?m)^## " + re.escape(heading) + r"\s*\n.*?(?=^## |\Z)"
                if len(re.findall(pattern, body, flags=re.S)) != 1:
                    raise ConflictError(f"方法章节不唯一或缺失：{heading}")
                body = re.sub(pattern, lambda _: f"## {heading}\n\n{value.strip()}\n\n", body, flags=re.S)
                if key == 'applicable_scenarios':
                    meta[key] = value.strip()
            self._method_index_text()
            backup_dir = self.backups_dir / ("method-revision-" + uuid.uuid4().hex)
            backup_dir.mkdir(parents=True)
            backup = backup_dir / path.name
            shutil.copy2(path, backup)
            _atomic_write(backup_dir / 'revision.json', json.dumps({"method_id": method['method_id'], "source": str(path), "expected_hash": method['hash'], "reason": payload['reason'], "changes": changes}, ensure_ascii=False, indent=2))
            meta.update(revision=int(meta.get('revision', 0)) + 1, previous_hash=method['hash'], updated_at=_now().isoformat(), revision_reason=payload['reason'], revision_backup=str(backup))
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
            self.refresh_method_index()
            return {**self._read_experience_card(path), "backup": str(backup)}

    def close_project(self, project: str, *, allow_open: bool = False, summary: str = "") -> dict[str, Any]:
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        open_tasks = [item for item in self.list_tasks(project_meta["project_id"]) if item.get("status") != "已完成"]
        open_issues = [item for item in self.list_issues(project_meta["project_id"]) if item.get("status") not in {"已解决", "已接受"}]
        if (open_tasks or open_issues) and not allow_open:
            raise WorklogError("项目仍有未完成任务或待处理问题；请先完成，或明确允许带遗留事项结项")
        self.initialize_project_experience(project_meta["project_id"])
        review = self._project_folder(project_meta) / "复盘" / f"{self._safe_name(project_meta['name'], '项目')}-项目复盘.md"
        content = "\n".join([
            "---", _safe_yaml({
                "type": "project-retrospective", "project_id": project_meta["project_id"],
                "project_name": project_meta["name"], "status": "已结项",
                "closed_at": _now().isoformat(), "allow_open": allow_open,
            }), "---", "", f"# {project_meta['name']} · 项目复盘", "",
            "## 项目结论", "", summary or "项目已完成，待补充结论。", "",
            "## 已验证经验", "",
            *( [f"- [[../问题与决策/{Path(item['path']).stem}|{Path(item['path']).stem}]]" for item in self.list_issues(project_meta["project_id"]) if item.get("status") == "已解决"] or ["- 待补充"] ), "",
            "## 遗留事项", "",
            *( [f"- 任务：{item['title']}（{item['status']}）" for item in open_tasks] + [f"- 问题：{Path(item['path']).stem}（{item['status']}）" for item in open_issues] or ["- 无"] ), "",
            "## 方法论候选", "", "- 见 `方法论候选` 目录；确认后再进入方法论库。", "",
        ])
        _atomic_write(review, content)
        self.update_project(project_meta["project_id"], {"project_status": "已完成"})
        return {"project": self.get_project(project_meta["project_id"]), "review_path": str(review), "open_tasks": open_tasks, "open_issues": open_issues}

    def revise_node(self, node_id: str, changes: dict[str, Any], expected_hash: str) -> dict[str, Any]:
        old = self.get_node(node_id)
        if not old:
            raise WorklogError(f"节点不存在：{node_id}")
        payload = {
            "project": changes.get("project_id") or old["project_id"],
            "summary": changes.get("summary", old["summary"]),
            "status": changes.get("status", old["status"]),
            "next_action": changes.get("next_action", old.get("next_action", "")),
            "kind": changes.get("kind", old["kind"]),
            "source": changes.get("source", old["source"]),
            "occurred_at": changes.get("occurred_at", old["occurred_at"]),
            "time_precision": changes.get("time_precision", old.get("time_precision", "minute")),
            "related_path": changes.get("related_path", old.get("related_path", "")),
            "include_in_report": changes.get("include_in_report", old.get("include_in_report", True)),
            "supersedes": node_id,
            "revision": int(old.get("revision", 1)) + 1,
            "expected_source_hash": expected_hash,
            "lane_id": changes.get("lane_id", old.get("lane_id", "main")),
            "lane_name": changes.get("lane_name", old.get("lane_name", "主线")),
            "track_type": changes.get("track_type", old.get("track_type", "主线")),
            "relation_type": changes.get("relation_type", old.get("relation_type", "顺序")),
            "predecessor_ids": changes.get("predecessor_ids", old.get("predecessor_ids", [])),
            "parent_node_id": changes.get("parent_node_id", old.get("parent_node_id", "")),
            "start_time": changes.get("start_time", old.get("start_time", old["occurred_at"])),
            "end_time": changes.get("end_time", old.get("end_time", "")),
            "keep_open": changes.get("keep_open", old.get("keep_open", False)),
            "task_id": changes.get("task_id", old.get("task_id", "")),
            "task_status": changes.get("task_status", ""),
            "intent_type": changes.get("intent_type", old.get("intent_type", "")),
            "review_flags": changes.get("review_flags", old.get("review_flags", [])),
            "review_state": changes.get("review_state", old.get("review_state", "已确认")),
            "excluded": old.get("excluded", False),
            "exclusion_reason": old.get("exclusion_reason", ""),
            "source_ref": changes.get("source_ref", ""),
        }
        return self.record_node(**payload, check_source_ref=False)

    def exclude_node(self, node_id: str, expected_hash: str, reason: str) -> dict[str, Any]:
        """Exclude one mistaken record and its revision chain without deleting audit history."""
        if not reason.strip():
            raise WorklogError("排除原因不能为空")
        selected = self.get_node(node_id)
        if not selected:
            raise WorklogError(f"节点不存在：{node_id}")
        if selected.get("hash") != expected_hash:
            raise ConflictError("原节点已在 Obsidian 中发生变化，请重新加载后再处理")

        with self.mutation_lock():
            history = self.list_nodes(effective_only=False)
            by_id = {str(item.get("node_id") or ""): item for item in history}
            links: dict[str, set[str]] = defaultdict(set)
            for item in history:
                current_id = str(item.get("node_id") or "")
                previous_id = str(item.get("supersedes") or "")
                if current_id and previous_id and previous_id in by_id:
                    links[current_id].add(previous_id)
                    links[previous_id].add(current_id)

            chain_ids: set[str] = set()
            pending = [node_id]
            while pending:
                current_id = pending.pop()
                if current_id in chain_ids:
                    continue
                chain_ids.add(current_id)
                pending.extend(links.get(current_id, set()) - chain_ids)
            chain = [by_id[item_id] for item_id in chain_ids if item_id in by_id]
            project_ids = {str(item.get("project_id") or "") for item in chain}
            task_ids = {str(item.get("task_id") or "") for item in chain}
            if len(project_ids) != 1 or len(task_ids) != 1:
                raise WorklogError("修订链跨越多个项目或任务，停止自动排除，请先人工核查")

            stamp = _now().strftime("%Y%m%d-%H%M%S")
            backup_dir = self.backups_dir / f"node-exclusion-{stamp}-{node_id[-8:]}"
            backup_dir.mkdir(parents=True, exist_ok=False)

            def backup(path: Path) -> None:
                if not path.exists():
                    return
                try:
                    relative = path.relative_to(self.vault_root)
                except ValueError:
                    relative = Path("external") / path.name
                target = backup_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

            project_id = next(iter(project_ids))
            project = self.get_project(project_id)
            task_id = next(iter(task_ids))
            task = self.get_task(task_id) if task_id else None
            for item in chain:
                backup(Path(item["path"]))
            if task:
                backup(Path(task["path"]))
            if project:
                backup(Path(project["path"]))
                overview = self._overview_file(project)
                if overview:
                    backup(overview)
                backup(self._canvas_path(project["name"]))

            excluded_at = _now().isoformat()
            for item in chain:
                self._rewrite_node_meta(item, {
                    "excluded": True,
                    "include_in_report": False,
                    "exclusion_reason": reason.strip(),
                    "excluded_at": excluded_at,
                })
            self._invalidate_node_cache()

            previous_latest = str((task or {}).get("latest_node_id") or "")
            effective_task_nodes = [
                item for item in self.list_nodes()
                if task_id and item.get("task_id") == task_id
            ]
            latest = max(
                effective_task_nodes,
                key=lambda item: (
                    str(item.get("occurred_at") or ""), str(item.get("created_at") or ""),
                    int(item.get("revision") or 0), str(item.get("node_id") or ""),
                ),
                default=None,
            )
            new_latest = str((latest or {}).get("node_id") or "")
            if task and previous_latest != new_latest:
                self.update_task(task_id, {"latest_node_id": new_latest}, sync_project=False)

            if project:
                with self.timed_stage("sync_project_state"):
                    self._sync_project_status_from_tasks(project_id)
                with self.timed_stage("sync_overview"):
                    self.sync_overview(project_id)
                with self.timed_stage("sync_native_views"):
                    self.sync_native_views(project_id)
                with self.timed_stage("write_work_panorama"):
                    self.write_work_panorama_pages()
            with self.timed_stage("post_mutation_audit"):
                audit = self.audit_integrity()
            return {
                "node_id": node_id,
                "excluded_node_ids": sorted(chain_ids),
                "excluded_count": len(chain),
                "reason": reason.strip(),
                "backup_dir": str(backup_dir),
                "task_id": task_id,
                "previous_latest_node_id": previous_latest,
                "latest_node_id": new_latest,
                "project": (project or {}).get("name", ""),
                "audit": audit,
            }

    @_timed_method
    def record_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.mutation_lock():
            return self._record_intent(payload)

    def _record_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent_type = str(payload.get("intent_type") or "").strip()
        if intent_type not in VALID_INTENT_TYPES:
            raise WorklogError(f"intent_type必须是：{'、'.join(sorted(VALID_INTENT_TYPES))}")
        project_ref = str(payload.get("project_id") or payload.get("project") or "").strip()
        project = self.get_project(project_ref)
        if not project:
            raise UnknownProjectError(f"项目未登记：{project_ref}。请先确认归属，不要静默新建相似项目。")
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            raise WorklogError("工作进展不能为空")

        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if idempotency_key:
            for existing in self.list_nodes(effective_only=False):
                if existing.get("idempotency_key") != idempotency_key:
                    continue
                expected_task_id = str(payload.get("task_id") or "").strip()
                mismatched = (
                    existing.get("project_id") != project["project_id"]
                    or existing.get("summary") != summary
                    or existing.get("status") != str(payload.get("node_status") or payload.get("status") or "进行中")
                    or (expected_task_id and existing.get("task_id") != expected_task_id)
                    or existing.get("excluded")
                )
                if mismatched:
                    raise ConflictError("幂等键已用于不同的工作记录，请重新生成同步草稿")
                replay = dict(existing)
                replay["idempotent_replay"] = True
                task = self.get_task(str(existing.get("task_id") or ""))
                if not task:
                    raise ConflictError("幂等写入命中的原任务已不存在，请先核对 Obsidian 完整性")
                return {
                    "intent_type": str(existing.get("intent_type") or intent_type),
                    "project": self.get_project(project["project_id"]),
                    "task": task,
                    "node": replay,
                    "idempotent_replay": True,
                }

        task_ref = str(payload.get("task_id") or payload.get("task_title") or "").strip()
        task = self.get_task(task_ref, project["project_id"]) if task_ref else None
        create_intents = {"新任务", "支线", "单次事项"}
        if not task and intent_type in create_intents:
            task_title = str(payload.get("task_title") or summary.splitlines()[0]).strip()[:100]
            task = self.create_task(
                project=project["project_id"],
                title=task_title,
                status=str(payload.get("task_status") or "进行中"),
                description=str(payload.get("task_description") or ""),
                required=_as_bool(payload.get("required", intent_type != "单次事项")),
                track_type="支线" if intent_type == "支线" else str(payload.get("track_type") or "主线"),
                lane_name=str(payload.get("lane_name") or task_title),
                parent_task_id=str(payload.get("parent_task_id") or ""),
                predecessor_task_ids=payload.get("predecessor_task_ids") or [],
                related_path=str(payload.get("related_path") or ""),
                source=str(payload.get("source") or "手工记录"),
            )
        if not task:
            open_tasks = self.list_tasks(project["project_id"], {"待开始", "进行中", "等待中", "阻塞", "待确认"})
            if len(open_tasks) == 1 and not task_ref:
                task = open_tasks[0]
            else:
                choices = "、".join(f"{item['title']}({item['task_id']})" for item in open_tasks[:8]) or "无"
                raise WorklogError(f"无法确定要更新的任务，请提供task_id。当前未完成任务：{choices}")

        node_status = str(payload.get("node_status") or payload.get("status") or "进行中")
        task_status = str(payload.get("task_status") or "")
        kind = str(payload.get("kind") or ("产出" if intent_type == "成果" else ("状态变化" if intent_type == "状态变化" else "进展")))
        track_type = str(payload.get("track_type") or task.get("track_type") or "主线")
        relation_type = str(payload.get("relation_type") or ("分叉" if intent_type == "支线" else "顺序"))
        node = self.record_node(
            project=project["project_id"],
            summary=summary,
            status=node_status,
            next_action=str(payload.get("next_action") or ""),
            kind=kind,
            source=str(payload.get("source") or "手工记录"),
            occurred_at=payload.get("occurred_at"),
            related_path=str(payload.get("related_path") or ""),
            include_in_report=_as_bool(payload.get("include_in_report", True)),
            idempotency_key=idempotency_key,
            lane_id=str(payload.get("lane_id") or ""),
            lane_name=str(payload.get("lane_name") or task.get("lane_name") or ""),
            track_type=track_type,
            relation_type=relation_type,
            predecessor_ids=payload.get("predecessor_ids") or [],
            parent_node_id=str(payload.get("parent_node_id") or ""),
            start_time=payload.get("start_time"),
            end_time=payload.get("end_time"),
            keep_open=_as_bool(payload.get("keep_open", False)),
            task_id=task["task_id"],
            task_status=task_status,
            intent_type=intent_type,
        )
        return {
            "intent_type": intent_type,
            "project": self.get_project(project["project_id"]),
            "task": self.get_task(task["task_id"]),
            "node": node,
            "idempotent_replay": bool(node.get("idempotent_replay", False)),
        }

    @staticmethod
    def _public_record(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key not in {"path", "hash"}}

    def project_context(self, project: str, recent_limit: int = 12, max_chars: int = 18000) -> dict[str, Any]:
        project_meta = self.get_project(project)
        if not project_meta:
            raise UnknownProjectError(f"项目未登记：{project}")
        overview_text = ""
        overview = self._overview_file(project_meta)
        if overview and overview.exists():
            overview_text = overview.read_text(encoding="utf-8")
            overview_text = re.sub(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), "", overview_text, flags=re.S)
            overview_text = overview_text.strip()[: max(2000, max_chars // 2)]
        tasks = self.tasks_with_current_progress(self.list_tasks(project_meta["project_id"]))
        nodes = self.list_nodes(project_id=project_meta["project_id"])[:recent_limit]
        graph = self.project_graph(project_meta["project_id"])
        issues = self.list_issues(project_meta["project_id"])
        methods = self.list_methods(status="正式")
        return {
            "project": self._public_record(project_meta),
            "overview": overview_text,
            "overview_hash": _file_hash(overview) if overview and overview.exists() else "",
            "open_tasks": [self._public_record(item) for item in tasks if item.get("status") != "已完成"],
            "completed_tasks": [self._public_record(item) for item in tasks if item.get("status") == "已完成"][:10],
            "recent_nodes": [self._public_record(item) for item in nodes],
            "work_lines": graph["lanes"],
            "review_count": graph["review_count"],
            "issues": [self._public_record(item) for item in issues[:12]],
            "available_methods": [self._public_record(item) for item in methods[:20]],
        }

    def revise_overview(self, project: str, expected_hash: str, replacements: list[dict[str, str]], reason: str) -> dict[str, Any]:
        """Apply reviewed, exact edits with conflict detection and a version snapshot."""
        with self.mutation_lock():
            meta = self.get_project(project)
            if not meta:
                raise UnknownProjectError(f"项目未登记：{project}")
            path = self._overview_file(meta)
            if not path or not path.exists() or not path.resolve().is_relative_to(self.vault_root.resolve()):
                raise WorklogError("项目总览必须存在且位于Vault内")
            if not expected_hash or _file_hash(path) != expected_hash:
                raise ConflictError("项目总览已变化，请重新读取后再修订")
            if not reason.strip() or not isinstance(replacements, list) or not replacements:
                raise WorklogError("必须提供修订原因与精确替换列表")
            original = path.read_text(encoding="utf-8")
            updated = original
            for item in replacements:
                old, new = item.get("old"), item.get("new")
                if not isinstance(old, str) or not old or not isinstance(new, str) or updated.count(old) != 1:
                    raise ConflictError("每个待替换片段必须唯一匹配，未写入任何修改")
                if any(marker in old or marker in new for marker in (MANAGED_START, MANAGED_END)):
                    raise WorklogError("系统受控区请通过工作节点同步")
                updated = updated.replace(old, new, 1)
            if updated == original:
                return {"changed": False, "project": meta["name"]}
            backup_dir = self.backups_dir / ("overview-revision-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            backup_dir.mkdir(parents=True)
            backup = backup_dir / path.name
            shutil.copy2(path, backup)
            _atomic_write(backup_dir / "revision.json", json.dumps({
                "project": meta["name"], "source": str(path), "expected_hash": expected_hash,
                "reason": reason, "replacements": replacements,
            }, ensure_ascii=False, indent=2))
            _atomic_write(path, updated)
            self.sync_overview(meta["project_id"])
            return {"changed": True, "project": meta["name"], "backup": str(backup), "hash": _file_hash(path)}

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            raise WorklogError("搜索内容不能为空")
        normalized = _search_normalize(query)
        results: list[dict[str, Any]] = []

        def add(kind: str, title: str, haystack: str, **extra: Any) -> None:
            compact = _search_normalize(haystack)
            if normalized not in compact and query.lower() not in haystack.lower():
                return
            normalized_title = _search_normalize(title)
            score = 100 if normalized == normalized_title else (70 if normalized in normalized_title else 40)
            results.append({"kind": kind, "title": title, "score": score, "snippet": haystack[:240], **extra})

        for project in self.list_projects():
            add(
                "project", project["name"],
                " ".join([project["name"], *(project.get("aliases") or []), project.get("latest_summary", "")]),
                project_id=project["project_id"], overview_path=project.get("overview_path", ""),
            )
        for task in self.list_tasks():
            add(
                "task", task["title"],
                " ".join([task["title"], *(task.get("aliases") or []), task.get("description", "")]),
                task_id=task["task_id"], project_id=task["project_id"], status=task["status"],
            )
        for node in self.list_nodes():
            add(
                "node", node.get("summary", ""),
                " ".join([node.get("summary", ""), node.get("next_action", ""), node.get("related_path", "")]),
                node_id=node["node_id"], project_id=node["project_id"], occurred_at=node["occurred_at"],
            )
        for issue in self.list_issues():
            title = Path(issue["path"]).stem
            add(
                "issue", title,
                " ".join([title, issue.get("stage", ""), issue.get("body", "")]),
                issue_id=issue.get("issue_id", ""), project_id=issue.get("project_id", ""), status=issue.get("status", ""),
            )
        for method in self.list_methods(status=""):
            title = Path(method["path"]).stem
            add(
                "method", title,
                " ".join([title, method.get("applicable_scenarios", ""), method.get("body", "")]),
                method_id=method.get("method_id", ""), status=method.get("status", ""),
            )
        for card in self.list_candidates():
            add("candidate", Path(card["path"]).stem, card["body"],
                candidate_id=card.get("candidate_id"), project_id=card.get("project_id"),
                status=card.get("status"), path=Path(card["path"]).relative_to(self.vault_root).as_posix())
        skip_root = self.root / "备份"
        for path in self.vault_root.rglob("*.md"):
            if ".obsidian" in path.parts or skip_root in path.parents or self.projects_dir in path.parents or self.tasks_dir in path.parents or self.nodes_dir in path.parents:
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if text.startswith("---\n"):
                meta, _ = _parse_frontmatter(path)
                if meta.get("type") in {"project-issue", "method-candidate", "reusable-method"}:
                    continue
            add("page", path.stem, text, path=path.relative_to(self.vault_root).as_posix())
        return sorted(results, key=lambda item: (item["score"], item["kind"] == "project"), reverse=True)[: max(1, min(limit, 100))]

    def project_graph(
        self,
        project_id: str,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if not project:
            raise UnknownProjectError(f"项目不存在：{project_id}")
        range_start = date.fromisoformat(start) if isinstance(start, str) and start else start
        range_end = date.fromisoformat(end) if isinstance(end, str) and end else end
        all_history = self.list_nodes(effective_only=False, project_id=project["project_id"])
        replacements = {item.get("supersedes"): item["node_id"] for item in all_history if item.get("supersedes")}

        def effective_id(node_id: str) -> str:
            seen: set[str] = set()
            while node_id in replacements and node_id not in seen:
                seen.add(node_id)
                node_id = replacements[node_id]
            return node_id

        project_nodes = self.list_nodes(project_id=project["project_id"])
        visible: list[dict[str, Any]] = []
        today = date.today()
        for item in sorted(project_nodes, key=lambda value: (value.get("start_time") or value.get("occurred_at", ""), value["node_id"])):
            start_date = date.fromisoformat(str(item.get("start_time") or item["occurred_at"])[:10])
            raw_end = item.get("end_time")
            end_date = date.fromisoformat(str(raw_end)[:10]) if raw_end else (today if item.get("status") != "已完成" else start_date)
            if range_start and end_date < range_start:
                continue
            if range_end and start_date > range_end:
                continue
            node = dict(item)
            node["display_start"] = start_date.isoformat()
            node["display_end"] = max(start_date, end_date).isoformat()
            node["predecessor_ids"] = [effective_id(value) for value in item.get("predecessor_ids", [])]
            node["parent_node_id"] = effective_id(item.get("parent_node_id", "")) if item.get("parent_node_id") else ""
            visible.append(node)
        visible_ids = {item["node_id"] for item in visible}
        edges = []
        for item in visible:
            for predecessor in item.get("predecessor_ids", []):
                if predecessor in visible_ids and predecessor != item["node_id"]:
                    edges.append({
                        "id": hashlib.sha1(f"{predecessor}>{item['node_id']}".encode("utf-8")).hexdigest()[:16],
                        "from": predecessor,
                        "to": item["node_id"],
                        "type": item.get("relation_type", "顺序"),
                    })
        lane_map: dict[str, dict[str, Any]] = {}
        for item in visible:
            lane = lane_map.setdefault(item.get("lane_id", "main"), {
                "lane_id": item.get("lane_id", "main"),
                "lane_name": item.get("lane_name", "主线"),
                "track_type": item.get("track_type", "主线"),
                "nodes": [],
            })
            lane["nodes"].append(item["node_id"])
        lanes = sorted(lane_map.values(), key=lambda item: (item["track_type"] != "主线", item["lane_name"]))
        return {
            "project": {
                "project_id": project["project_id"],
                "name": project["name"],
                "major_work": project.get("major_work", "其他"),
                "key_point": project.get("key_point", "其他支持事项"),
                "project_status": project.get("project_status", "进行中"),
                "overview_path": project.get("overview_path", ""),
            },
            "lanes": lanes,
            "nodes": visible,
            "edges": edges,
            "review_count": sum(item.get("review_state") == "待确认" for item in visible),
        }

    def _canvas_payload(self, graph: dict[str, Any]) -> dict[str, Any]:
        nodes = graph["nodes"]
        lanes = graph["lanes"] or [{"lane_id": "main", "lane_name": "主线", "track_type": "主线", "nodes": []}]
        positions: dict[str, tuple[int, int]] = {}
        canvas_nodes: list[dict[str, Any]] = [{
            "id": hashlib.sha1((graph["project"]["project_id"] + "warning").encode()).hexdigest()[:16],
            "type": "text",
            "x": 0,
            "y": -190,
            "width": 720,
            "height": 110,
            "color": "3",
            "text": f"# {graph['project']['name']} · 工作关系图\n\n由周报系统自动生成，请在网页看板中调整关系。",
        }]
        node_by_id = {item["node_id"]: item for item in nodes}
        for lane_index, lane in enumerate(lanes):
            lane_nodes = [node_by_id[node_id] for node_id in lane["nodes"] if node_id in node_by_id]
            y = lane_index * 310
            width = max(460, len(lane_nodes) * 360 + 80)
            group_id = hashlib.sha1((graph["project"]["project_id"] + lane["lane_id"]).encode()).hexdigest()[:16]
            canvas_nodes.append({
                "id": group_id,
                "type": "group",
                "x": -40,
                "y": y - 45,
                "width": width,
                "height": 245,
                "label": f"{lane['track_type']} · {lane['lane_name']}",
                "color": "5" if lane["track_type"] == "主线" else ("6" if lane["track_type"] == "支线" else "4"),
            })
            for index, item in enumerate(lane_nodes):
                x = index * 360 + 20
                positions[item["node_id"]] = (x, y)
                try:
                    relative = Path(item["path"]).relative_to(self.vault_root).as_posix()
                except ValueError:
                    relative = Path(item["path"]).as_posix()
                color = {"已完成": "4", "阻塞": "1", "等待中": "2", "待确认": "3", "待开始": "5"}.get(item.get("status"), "5")
                canvas_nodes.append({
                    "id": hashlib.sha1(item["node_id"].encode()).hexdigest()[:16],
                    "type": "file",
                    "x": x,
                    "y": y,
                    "width": 300,
                    "height": 150,
                    "file": relative,
                    "color": color,
                })
        canvas_edges = []
        for edge in graph["edges"]:
            if edge["from"] not in positions or edge["to"] not in positions:
                continue
            canvas_edges.append({
                "id": edge["id"],
                "fromNode": hashlib.sha1(edge["from"].encode()).hexdigest()[:16],
                "fromSide": "right",
                "toNode": hashlib.sha1(edge["to"].encode()).hexdigest()[:16],
                "toSide": "left",
                "toEnd": "arrow",
                "label": edge["type"],
            })
        return {"nodes": canvas_nodes, "edges": canvas_edges}

    @_timed_method
    def sync_native_views(self, project_id: str | None = None) -> None:
        self._write_base_views()
        projects = [self.get_project(project_id)] if project_id else self.list_projects()
        for project in (item for item in projects if item):
            graph = self.project_graph(project["project_id"])
            latest = max(
                graph["nodes"],
                key=lambda item: (
                    str(item.get("occurred_at") or ""),
                    str(item.get("created_at") or ""),
                    int(item.get("revision") or 0),
                    str(item.get("node_id") or ""),
                ),
                default=None,
            )
            project_path = Path(project["path"])
            meta, body = _parse_frontmatter(project_path)
            project_tasks = self.list_tasks(project["project_id"])
            derived = {
                "latest_summary": latest.get("summary", "") if latest else "",
                "next_action": latest.get("next_action", "") if latest else "",
                "last_activity_at": latest.get("occurred_at", "") if latest else "",
                "review_count": graph["review_count"],
                "open_task_count": sum(item.get("status") != "已完成" for item in project_tasks),
                "task_count": len(project_tasks),
                "canvas_path": self._canvas_path(project["name"]).relative_to(self.vault_root).as_posix(),
            }
            if any(meta.get(key) != value for key, value in derived.items()):
                meta.update(derived)
                meta["project_status"] = meta.get("project_status") or meta.pop("status", "进行中")
                _atomic_write(project_path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
                self._project_cache.pop(str(project_path), None)
            canvas = self._canvas_path(project["name"])
            content = json.dumps(self._canvas_payload(graph), ensure_ascii=False, indent=2) + "\n"
            if not canvas.exists() or canvas.read_text(encoding="utf-8") != content:
                _atomic_write(canvas, content)
            # All view refreshes share the same overview synchronization path.
            self.sync_overview(project["project_id"])

    def audit_integrity(self) -> dict[str, Any]:
        """Read-only integrity audit for IDs, references, and derived project state."""
        projects = self.list_projects()
        tasks = self.list_tasks()
        nodes = self.list_nodes(effective_only=False)
        issues: dict[str, list[Any]] = defaultdict(list)

        def duplicates(items: list[dict[str, Any]], key: str) -> list[str]:
            counts = Counter(str(item.get(key) or "") for item in items)
            return sorted(value for value, count in counts.items() if value and count > 1)

        for label, items, key in (
            ("duplicate_project_ids", projects, "project_id"),
            ("duplicate_project_names", projects, "name"),
            ("duplicate_task_ids", tasks, "task_id"),
            ("duplicate_node_ids", nodes, "node_id"),
        ):
            issues[label].extend(duplicates(items, key))

        project_by_id = {item.get("project_id"): item for item in projects}
        task_by_id = {item.get("task_id"): item for item in tasks}
        node_by_id = {item.get("node_id"): item for item in nodes}
        for task in tasks:
            project = project_by_id.get(task.get("project_id"))
            if not project:
                issues["orphan_tasks"].append({"task_id": task.get("task_id"), "path": task.get("path")})
            elif task.get("project_name") and task.get("project_name") != project.get("name"):
                issues["task_project_name_mismatches"].append({"task_id": task.get("task_id")})
        for node in nodes:
            project = project_by_id.get(node.get("project_id"))
            excluded = bool(node.get("excluded"))
            if not project:
                bucket = "excluded_orphan_nodes" if excluded else "orphan_nodes"
                issues[bucket].append({"node_id": node.get("node_id"), "path": node.get("path")})
            elif node.get("project_name") and node.get("project_name") != project.get("name"):
                issues["node_project_name_mismatches"].append({"node_id": node.get("node_id")})
            task_id = node.get("task_id")
            if task_id and task_id not in task_by_id:
                bucket = "excluded_nodes_missing_tasks" if excluded else "nodes_missing_tasks"
                issues[bucket].append({"node_id": node.get("node_id"), "task_id": task_id})
            elif task_id and task_by_id[task_id].get("project_id") != node.get("project_id"):
                issues["node_task_project_mismatches"].append({"node_id": node.get("node_id"), "task_id": task_id})
            for predecessor_id in node.get("predecessor_ids") or []:
                predecessor = node_by_id.get(predecessor_id)
                if not predecessor:
                    issues["missing_predecessors"].append({"node_id": node.get("node_id"), "predecessor_id": predecessor_id})
                elif predecessor.get("project_id") != node.get("project_id"):
                    issues["cross_project_predecessors"].append({"node_id": node.get("node_id"), "predecessor_id": predecessor_id})

        superseded = {item.get("supersedes") for item in nodes if item.get("supersedes")}
        effective = [item for item in nodes if not item.get("excluded") and item.get("node_id") not in superseded]
        nodes_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
        nodes_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in effective:
            nodes_by_project[str(node.get("project_id") or "")].append(node)
            if node.get("task_id"):
                nodes_by_task[str(node.get("task_id"))].append(node)
        for task in tasks:
            task_nodes = nodes_by_task.get(str(task.get("task_id") or ""), [])
            latest = max(
                task_nodes,
                key=lambda item: (
                    str(item.get("occurred_at") or ""), str(item.get("created_at") or ""),
                    int(item.get("revision") or 0), str(item.get("node_id") or ""),
                ),
                default=None,
            )
            expected_latest = str((latest or {}).get("node_id") or "")
            stored_latest = str(task.get("latest_node_id") or "")
            if stored_latest != expected_latest:
                issues["task_latest_node_mismatches"].append({
                    "task_id": task.get("task_id"), "stored_latest_node_id": stored_latest,
                    "expected_latest_node_id": expected_latest,
                })
        for project in projects:
            project_nodes = nodes_by_project.get(str(project.get("project_id") or ""), [])
            latest = max(
                project_nodes,
                key=lambda item: (
                    str(item.get("occurred_at") or ""), str(item.get("created_at") or ""),
                    int(item.get("revision") or 0), str(item.get("node_id") or ""),
                ),
                default=None,
            )
            expected_summary = (latest or {}).get("summary") or ""
            expected_next = (latest or {}).get("next_action") or ""
            if (project.get("latest_summary") or "") != expected_summary or (project.get("next_action") or "") != expected_next:
                issues["derived_latest_mismatches"].append({
                    "project": project.get("name"), "stored_summary": project.get("latest_summary") or "",
                    "expected_summary": expected_summary, "stored_next_action": project.get("next_action") or "",
                    "expected_next_action": expected_next,
                })
            overview = self._overview_file(project)
            if overview and overview.exists():
                meta, body = _parse_frontmatter(overview)
                if str(meta.get("状态") or "") != str(project.get("project_status") or "进行中"):
                    issues["overview_status_mismatches"].append({
                        "project": project["name"], "path": str(overview),
                        "stored_status": meta.get("状态"), "expected_status": project.get("project_status"),
                    })
                match = re.search(re.escape(MANAGED_START) + r"(.*?)" + re.escape(MANAGED_END), body, re.S)
                managed = match.group(1) if match else ""
                expected_fields = {
                    "项目状态": str(project.get("project_status") or "进行中"),
                    "最新进度": _clean_cell(expected_summary or "暂无记录"),
                    "下一步动作": _clean_cell(expected_next) if expected_next else "无",
                }
                stale_fields = [key for key, value in expected_fields.items()
                                if f"- **{key}**：{value}\n" not in managed]
                project_tasks = [task for task in tasks if task.get("project_id") == project["project_id"]]
                open_tasks = [task for task in project_tasks if task.get("status") != "已完成"]
                expected_tasks = "\n".join(
                    [f"- `{task.get('status', '待开始')}` {task.get('title', '')}" for task in open_tasks[:10]]
                    or ["- 暂无未完成任务"])
                task_section = re.search(r"### 当前任务\n\n(.*?)\n\n###", managed, re.S)
                if not task_section or task_section.group(1) != expected_tasks:
                    stale_fields.append("当前任务")
                if stale_fields:
                    issues["overview_sync_mismatches"].append({
                        "project": project["name"], "path": str(overview), "fields": stale_fields,
                    })

        warnings = {key: value for key, value in issues.items() if value and key.startswith("excluded_")}
        state_warnings = [item for project in projects for item in completed_project_findings(project, tasks)]
        if state_warnings:
            warnings["completed_project_open_tasks"] = state_warnings
        errors = {key: value for key, value in issues.items() if value and not key.startswith("excluded_")}
        return {
            "ok": not errors,
            "counts": {"projects": len(projects), "tasks": len(tasks), "nodes": len(nodes)},
            "errors": errors,
            "warnings": warnings,
        }

    def repair_integrity(self, *, views_only: bool = False) -> dict[str, Any]:
        """Repair mechanical integrity faults while preserving an auditable copy."""
        with self.mutation_lock():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_dir = self.backups_dir / f"integrity-repair-{stamp}"
            if views_only:
                return self._repair_derived_views(backup_dir)
            changed: list[dict[str, Any]] = []
            nodes = self.list_nodes(effective_only=False)
            historical_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for node in nodes:
                key = str(node.get("original_node_id") or node.get("node_id") or "")
                historical_groups[key].append(node)

            def canonical_score(item: dict[str, Any]) -> tuple[int, int, str]:
                _, body = _parse_frontmatter(Path(item["path"]))
                return (int("## 本次进展" in body), len(str(item.get("summary") or "")), str(item["path"]))

            for original_id, candidates in historical_groups.items():
                if len(candidates) < 2 or not any(item.get("original_node_id") for item in candidates):
                    continue
                canonical = max(candidates, key=canonical_score)
                for candidate in candidates:
                    path = Path(candidate["path"])
                    relative = path.relative_to(self.vault_root)
                    target = backup_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    meta, body = _parse_frontmatter(path)
                    if candidate["path"] == canonical["path"]:
                        meta["node_id"] = original_id
                        meta.pop("original_node_id", None)
                        if meta.get("duplicate_of") == original_id:
                            meta.pop("duplicate_of", None)
                        if str(meta.get("exclusion_reason") or "").startswith("完整性修复：重复节点ID"):
                            meta["excluded"] = False
                            meta["include_in_report"] = True
                            meta["exclusion_reason"] = ""
                        meta["integrity_repaired_at"] = _now().isoformat()
                    else:
                        meta.update({
                            "node_id": f"node-audit-{hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:20]}",
                            "original_node_id": original_id,
                            "duplicate_of": original_id,
                            "excluded": True,
                            "include_in_report": False,
                            "exclusion_reason": f"完整性修复：重复节点ID，正式记录保留于 {canonical['path']}",
                            "integrity_repaired_at": _now().isoformat(),
                        })
                    _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
                    changed.append({"kind": "duplicate_canonical_reselection", "path": str(path), "backup": str(target)})

            self._invalidate_node_cache()
            nodes = self.list_nodes(effective_only=False)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for node in nodes:
                grouped[str(node.get("node_id") or "")].append(node)

            for node_id, duplicates in grouped.items():
                if not node_id or len(duplicates) < 2:
                    continue
                canonical = max(duplicates, key=canonical_score)
                for duplicate in duplicates:
                    if duplicate["path"] == canonical["path"]:
                        continue
                    path = Path(duplicate["path"])
                    relative = path.relative_to(self.vault_root)
                    target = backup_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    meta, body = _parse_frontmatter(path)
                    archival_id = f"node-audit-{hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:20]}"
                    meta.update({
                        "node_id": archival_id,
                        "original_node_id": node_id,
                        "duplicate_of": node_id,
                        "excluded": True,
                        "include_in_report": False,
                        "exclusion_reason": f"完整性修复：重复节点ID，正式记录保留于 {canonical['path']}",
                        "integrity_repaired_at": _now().isoformat(),
                    })
                    _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
                    changed.append({"kind": "duplicate_node_id", "path": str(path), "backup": str(target)})

            self._invalidate_node_cache()
            nodes = self.list_nodes(effective_only=False)
            known_ids = {str(item.get("node_id") or "") for item in nodes}
            for node in nodes:
                missing = [item for item in (node.get("predecessor_ids") or []) if item not in known_ids]
                if not missing:
                    continue
                path = Path(node["path"])
                relative = path.relative_to(self.vault_root)
                target = backup_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(path, target)
                meta, body = _parse_frontmatter(path)
                meta["predecessor_ids"] = [item for item in (meta.get("predecessor_ids") or []) if item not in missing]
                meta["missing_predecessor_ids"] = sorted(set((meta.get("missing_predecessor_ids") or []) + missing))
                meta["integrity_repaired_at"] = _now().isoformat()
                _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
                changed.append({"kind": "missing_predecessor", "path": str(path), "backup": str(target)})

            self._invalidate_node_cache()
            effective_nodes = self.list_nodes()
            effective_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for node in effective_nodes:
                if node.get("task_id"):
                    effective_by_task[str(node.get("task_id"))].append(node)
            for task in self.list_tasks():
                task_nodes = effective_by_task.get(str(task.get("task_id") or ""), [])
                latest = max(
                    task_nodes,
                    key=lambda item: (
                        str(item.get("occurred_at") or ""), str(item.get("created_at") or ""),
                        int(item.get("revision") or 0), str(item.get("node_id") or ""),
                    ),
                    default=None,
                )
                expected_latest = str((latest or {}).get("node_id") or "")
                if str(task.get("latest_node_id") or "") == expected_latest:
                    continue
                path = Path(task["path"])
                relative = path.relative_to(self.vault_root)
                target = backup_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(path, target)
                self.update_task(task["task_id"], {"latest_node_id": expected_latest}, sync_project=False)
                changed.append({"kind": "task_latest_node", "path": str(path), "backup": str(target)})

            views = self._repair_derived_views(backup_dir)
            return {**views, "changed": changed + views["changed"]}

    def _repair_derived_views(self, backup_dir: Path) -> dict[str, Any]:
        with self.mutation_lock():
            changed: list[dict[str, Any]] = []
            # Snapshot all derived files before refreshing; do not change business facts.
            sources = [Path(project["path"]) for project in self.list_projects()]
            sources += [path for project in self.list_projects()
                        if (path := self._overview_file(project)) and path.exists()]
            for folder in (self.canvas_dir, self.views_dir, self.panorama_dir):
                sources.extend(path for path in folder.rglob("*") if path.is_file())
            for path in sources:
                if not path.resolve().is_relative_to(self.vault_root.resolve()):
                    continue
                target = backup_dir / path.relative_to(self.vault_root)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            before_overviews = {str(path): _file_hash(path) for path in sources
                                if path.exists() and path.suffix == ".md"}
            self.sync_native_views()
            self.write_work_panorama_pages()
            for path, previous_hash in before_overviews.items():
                if _file_hash(Path(path)) != previous_hash:
                    changed.append({"kind": "derived_view_sync", "path": path,
                                    "backup": str(backup_dir / Path(path).relative_to(self.vault_root))})
            return {"backup_dir": str(backup_dir), "changed": changed, "audit": self.audit_integrity()}

    def _wikilink_for_project(self, project: dict[str, Any]) -> str:
        raw = str(project.get("overview_path") or "")
        target = raw[:-3] if raw.endswith(".md") else raw
        if not target:
            target = f"周报系统/项目台账/{_safe_filename(project['name'], '未命名项目')}"
        return f"[[{target}|{project['name']}]]"

    def _write_panorama_bases(self) -> None:
        bases = {
            "01-本月工作.base": """formulas:
  month: 'date(occurred_at).format("YYYY-MM")'
  happened: 'date(occurred_at).format("YYYY-MM-DD HH:mm")'
filters:
  and:
    - 'type == "weekly-node"'
    - 'excluded != true'
    - 'formula.month == now().format("YYYY-MM")'
properties:
  project_name:
    displayName: "工作事项"
  work_domain:
    displayName: "工作领域"
  summary:
    displayName: "进展"
  status:
    displayName: "状态"
  source:
    displayName: "来源"
  formula.happened:
    displayName: "时间"
views:
  - type: table
    name: "本月工作"
    groupBy:
      property: work_domain
      direction: ASC
    order:
      - formula.happened
      - project_name
      - summary
      - status
      - source
""",
            "02-工作日历.base": """filters:
  and:
    - 'type == "weekly-node"'
    - 'excluded != true'
formulas:
  day: 'date(occurred_at).format("YYYY-MM-DD")'
  time: 'date(occurred_at).format("HH:mm")'
properties:
  project_name:
    displayName: "工作事项"
  summary:
    displayName: "当天进展"
  status:
    displayName: "状态"
  formula.day:
    displayName: "日期"
  formula.time:
    displayName: "时间"
views:
  - type: table
    name: "每日工作记录"
    groupBy:
      property: formula.day
      direction: DESC
    order:
      - formula.day
      - formula.time
      - project_name
      - summary
      - status
""",
            "03-进行中项目.base": """filters:
  and:
    - 'type == "weekly-project"'
    - 'project_status != "已完成"'
    - 'display_in_views != false'
properties:
  name:
    displayName: "项目或工作"
  work_domain:
    displayName: "工作领域"
  work_category:
    displayName: "分类"
  work_type:
    displayName: "类型"
  project_status:
    displayName: "状态"
  latest_summary:
    displayName: "最新进展"
  next_action:
    displayName: "下一步"
views:
  - type: table
    name: "进行中工作"
    groupBy:
      property: work_domain
      direction: ASC
    order:
      - name
      - work_category
      - work_type
      - project_status
      - latest_summary
      - next_action
""",
            "04-待办与等待事项.base": """filters:
  and:
    - 'type == "work-task"'
    - or:
        - 'status == "待开始"'
        - 'status == "进行中"'
        - 'status == "等待中"'
        - 'status == "阻塞"'
        - 'status == "待确认"'
properties:
  project_name:
    displayName: "所属工作"
  title:
    displayName: "任务"
  status:
    displayName: "状态"
  work_domain:
    displayName: "领域"
  updated_at:
    displayName: "更新时间"
views:
  - type: table
    name: "待办与等待事项"
    groupBy:
      property: status
      direction: ASC
    order:
      - project_name
      - title
      - status
      - work_domain
      - updated_at
""",
            "05-成果索引.base": """filters:
  and:
    - 'type == "weekly-node"'
    - 'excluded != true'
    - 'kind == "产出"'
properties:
  project_name:
    displayName: "所属工作"
  summary:
    displayName: "成果"
  related_path:
    displayName: "关联文件"
  occurred_at:
    displayName: "完成时间"
  source:
    displayName: "来源"
views:
  - type: table
    name: "成果索引"
    order:
      - occurred_at
      - project_name
      - summary
      - related_path
      - source
""",
        }
        for name, content in bases.items():
            path = self.panorama_dir / name
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                _atomic_write(path, content)

    def write_work_panorama_pages(self) -> None:
        self._write_panorama_bases()
        projects = self.list_projects()
        tasks = self.list_tasks()
        nodes = self.list_nodes()
        today = date.today()
        month_prefix = today.strftime("%Y-%m")
        month_nodes = [item for item in nodes if str(item.get("occurred_at", "")).startswith(month_prefix)]
        visible_projects = [item for item in projects if item.get("display_in_views", True)]
        visible_ids = {item["project_id"] for item in visible_projects}
        open_tasks = [item for item in tasks if item.get("status") != "已完成"
                      and item.get("project_id") in visible_ids]
        active_projects = [item for item in visible_projects if item.get("project_status") != "已完成"]
        review = self.review_items()

        home = [
            "---", "type: 工作全景", "managed_by: obsidian-worklog", "---", "", "# 个人工作全景", "",
            "> 这里是工作入口。业务内容仍保存在原项目和工作节点中，本页只做聚合展示。", "",
            "## 当前概览", "",
            f"- 本月进展：**{len(month_nodes)}** 条",
            f"- 进行中项目与持续工作：**{len(active_projects)}** 项",
            f"- 未完成任务：**{len(open_tasks)}** 项",
            f"- 待确认记录：**{len(review)}** 条", "",
            "## 快速入口", "",
            "- [[00-工作全景/01-本月工作.base|本月工作]]",
            "- [[00-工作全景/02-工作日历.base|工作日历]]",
            "- [[00-工作全景/03-进行中项目.base|进行中项目]]",
            "- [[00-工作全景/04-待办与等待事项.base|待办与等待事项]]",
            "- [[00-工作全景/05-成果索引.base|成果索引]]",
            "- [[00-工作全景/06-待确认归档|待确认归档]]", "",
            "## 工作领域", "",
            "- [[项目管理/00-项目管理总览|项目管理]]",
            "- [[培训工作/00-培训工作总览|培训工作]]",
            "- [[体系评审/00-体系评审总览|体系评审]]",
            "- [[日常与专项工作/00-日常工作总览|日常与专项工作]]",
            "- [[方法论库/00-方法论库索引|方法论库]]", "",
        ]
        project_by_id = {item["project_id"]: item for item in visible_projects}
        node_by_id = {item["node_id"]: item for item in nodes}
        groups = {"本周行动（候选）": [], "等待反馈": [], "持续维护": []}
        for task in open_tasks:
            project = project_by_id[task["project_id"]]
            node = node_by_id.get(task.get("latest_node_id"), {})
            action = str(task.get("next_action") or node.get("next_action") or "").strip()
            if task.get("status") in {"等待中", "阻塞", "待确认"} or re.match(
                r"^(等|待收到|收到.*后|试点上线后|待.*(?:反馈|审批|上线|提供))", action
            ):
                group = "等待反馈"
            elif re.match(r"^(下月|每月|定期|持续维护|按需)", action) or (
                not action and project.get("project_mode") == "ongoing"
            ):
                group = "持续维护"
            else:
                group = "本周行动（候选）"
            task_path = Path(task["path"]).relative_to(self.vault_root).with_suffix("").as_posix()
            groups[group].append(
                f"- {self._wikilink_for_project(project)} / [[{task_path}|{_clean_cell(task['title'])}]]"
                f"：{_clean_cell(action) or '下一步待明确'}"
            )
        home.extend(["## 当前事项分区", "",
                     "> 按现有状态和下一步自动分区；行动栏是待安排候选，不代表已确认本周截止。长期项目中的具体动作仍单独列入行动或等待，不因项目长期而隐藏。", ""])
        for label, entries in groups.items():
            home.extend([f"### {label}（{len(entries)}项）", "", *(entries or ["暂无事项。"]), ""])
        _write_if_changed(self.panorama_dir / "00-工作全景-首页.md", "\n".join(home))

        review_lines = [
            "---", "type: 工作审核", "managed_by: obsidian-worklog", "---", "", "# 待确认归档", "",
            "![[周报系统/视图/待确认节点.base]]", "",
        ]
        if review:
            review_lines.extend(["## 待确认清单", ""])
            for item in review:
                review_lines.append(
                    f"- [[周报系统/工作节点/{str(item['occurred_at'])[:4]}/{str(item['occurred_at'])[5:7]}/{item['node_id']}|"
                    f"{str(item['occurred_at'])[:10]} · {item['project_name']}]]：{_clean_cell(item.get('summary', ''))}"
                )
        else:
            review_lines.append("当前没有待确认记录。")
        _write_if_changed(self.panorama_dir / "06-待确认归档.md", "\n".join(review_lines) + "\n")

        def write_domain(root: Path, title: str, domain: str, categories: list[str]) -> None:
            domain_projects = [
                item for item in visible_projects
                if item.get("work_domain") == domain or domain in (item.get("view_tags") or [])
            ]
            index = ["---", "type: 工作领域总览", "managed_by: obsidian-worklog", "---", "", f"# {title}", ""]
            cross_view_categories: dict[tuple[str, str], str] = {}
            for number, category in enumerate(categories, start=1):
                folder = root / f"{number:02d}-{category}"
                folder.mkdir(parents=True, exist_ok=True)
                items = [
                    item for item in domain_projects
                    if cross_view_categories.get((domain, item["name"]), item.get("work_category")) == category
                ]
                index.extend([f"## {category}", ""])
                category_page = ["---", "type: 工作分类索引", "managed_by: obsidian-worklog", "---", "", f"# {category}", ""]
                if not items:
                    index.append("- 暂无已登记工作")
                    category_page.append("暂无已登记工作。")
                for item in sorted(items, key=lambda value: value.get("last_activity_at", ""), reverse=True):
                    line = f"- {self._wikilink_for_project(item)} · {item.get('project_status', '进行中')} · {item.get('latest_summary') or '暂无进展'}"
                    index.append(line)
                    category_page.append(line)
                index.append("")
                _write_if_changed(folder / f"00-{category}索引.md", "\n".join(category_page) + "\n")
            _write_if_changed(root / f"00-{title}总览.md", "\n".join(index) + "\n")

        write_domain(
            self.win_dir, "培训工作", "培训工作",
            list(TRAINING_CATEGORIES),
        )
        write_domain(
            self.daily_dir, "日常工作", "日常与专项工作",
            list(DAILY_CATEGORIES),
        )

        project_items = [item for item in visible_projects if item.get("work_domain") == "项目管理"]
        project_index = ["---", "type: 工作领域总览", "managed_by: obsidian-worklog", "---", "", "# 项目管理总览", ""]
        project_groups = {}
        by_category: dict[str, list[dict[str, Any]]] = {}
        for item in project_items:
            group = project_groups.get(item.get("work_category", "其他项目"), "05-其他项目")
            by_category.setdefault(group, []).append(item)
        for folder_name, items in sorted(by_category.items()):
            title = folder_name.split("-", 1)[1]
            project_index.extend([f"## {title}", ""])
            category_page = ["---", "type: 工作分类索引", "managed_by: obsidian-worklog", "---", "", f"# {title}", ""]
            for item in sorted(items, key=lambda value: value.get("last_activity_at", ""), reverse=True):
                line = f"- {self._wikilink_for_project(item)} · {item.get('project_status', '进行中')} · {item.get('latest_summary') or '暂无进展'}"
                project_index.append(line)
                category_page.append(line)
            project_index.append("")
            folder = self.vault_root / "项目管理" / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            _write_if_changed(folder / f"00-{title}索引.md", "\n".join(category_page) + "\n")
        _write_if_changed(self.vault_root / "项目管理" / "00-项目管理总览.md", "\n".join(project_index) + "\n")

        system_overview = self.vault_root / "体系评审" / "00-体系评审总览.md"
        if system_overview.exists():
            system_items = [item for item in visible_projects if item.get("work_domain") == "体系评审"]
            managed = [DOMAIN_START, "## 当前体系事项", ""]
            for item in sorted(system_items, key=lambda value: value.get("last_activity_at", ""), reverse=True):
                managed.append(
                    f"- {self._wikilink_for_project(item)} · {item.get('project_status', '进行中')} · {item.get('latest_summary') or '暂无进展'}"
                )
            managed.extend(["", "> 本区块由工作全景自动汇总；历史里程碑和原始说明保留在上方。", DOMAIN_END])
            text = system_overview.read_text(encoding="utf-8")
            original_text = text
            text = _separate_domain_block(text)
            pattern = re.compile(re.escape(DOMAIN_START) + r".*?" + re.escape(DOMAIN_END), re.S)
            replacement = "\n".join(managed)
            updated = pattern.sub(replacement, text) if pattern.search(text) else text.rstrip() + "\n\n" + replacement + "\n"
            if updated != original_text:
                _atomic_write(system_overview, updated)

    def review_items(self) -> list[dict[str, Any]]:
        return [item for item in self.list_nodes() if item.get("review_state") == "待确认"]

    def resolve_review(self, decisions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        projects: set[str] = set()
        allowed = {"status", "track_type", "lane_id", "lane_name", "relation_type", "predecessor_ids", "parent_node_id", "include_in_report", "keep_open"}
        for decision in decisions:
            node = self.get_node(str(decision.get("node_id", "")))
            if not node:
                continue
            changes = {key: value for key, value in decision.items() if key in allowed}
            changes.update({"review_state": "已确认", "review_flags": []})
            self._rewrite_node_meta(node, changes)
            refreshed = self.get_node(node["node_id"])
            if refreshed:
                updated.append(refreshed)
                projects.add(refreshed["project_id"])
        for project_id in projects:
            self.sync_overview(project_id)
            self.sync_native_views(project_id)
        return updated

    def migrate_schema_v2(self) -> dict[str, Any]:
        current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) if self.schema_path.exists() else {}
        if int((current or {}).get("version", 1)) >= 2:
            return {"migrated": False, "version": 2, "review": len(self.review_items())}

        stamp = _now().strftime("%Y%m%d-%H%M%S")
        backup_root = self.backups_dir / f"schema-v1-{stamp}"
        for source in (self.projects_dir, self.nodes_dir):
            if source.exists():
                shutil.copytree(source, backup_root / source.name)

        for path in self.projects_dir.glob("*.md"):
            meta, body = _parse_frontmatter(path)
            if meta.get("type") != "weekly-project":
                continue
            old_status = meta.pop("status", None)
            meta["project_status"] = meta.get("project_status") or (
                old_status if old_status in VALID_PROJECT_STATUSES else "进行中"
            )
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")

        for path in self.nodes_dir.rglob("*.md"):
            meta, body = _parse_frontmatter(path)
            if meta.get("type") != "weekly-node":
                continue
            combined = " ".join(str(meta.get(key, "")) for key in ("source_ref", "related_path")) + " " + body
            track_type = meta.get("track_type") or ("子任务" if "子任务" in combined else ("支线" if any(word in combined for word in ("支线", "分支任务")) else "主线"))
            lane_name = meta.get("lane_name") or ("主线" if track_type == "主线" else track_type)
            meta.update({
                "lane_id": meta.get("lane_id") or ("main" if track_type == "主线" else f"lane-{hashlib.sha1((meta['project_id'] + lane_name).encode()).hexdigest()[:10]}"),
                "lane_name": lane_name,
                "track_type": track_type,
                "relation_type": meta.get("relation_type") or ("子任务" if track_type == "子任务" else ("分叉" if track_type == "支线" else "顺序")),
                "predecessor_ids": meta.get("predecessor_ids") or [],
                "parent_node_id": meta.get("parent_node_id") or "",
                "start_time": meta.get("start_time") or meta.get("occurred_at", ""),
                "end_time": meta.get("end_time") or (meta.get("occurred_at", "") if meta.get("status") == "已完成" else ""),
                "keep_open": _as_bool(meta.get("keep_open", False)),
                "review_flags": meta.get("review_flags") or [],
                "review_state": meta.get("review_state") or "已确认",
                "excluded": _as_bool(meta.get("excluded", False)),
                "exclusion_reason": meta.get("exclusion_reason", ""),
            })
            _atomic_write(path, f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}")
        self._invalidate_node_cache()

        completion_words = ("完成", "上线", "发布", "交付", "提交", "已配置", "已上传", "已开展")
        for project in self.list_projects():
            nodes = sorted(
                (item for item in self.list_nodes() if item.get("project_id") == project["project_id"]),
                key=lambda item: (item.get("start_time") or item.get("occurred_at", ""), item["node_id"]),
            )
            lanes: dict[str, list[dict[str, Any]]] = {}
            dates: dict[str, int] = {}
            for node in nodes:
                lanes.setdefault(node.get("lane_id", "main"), []).append(node)
                day = str(node.get("occurred_at", ""))[:10]
                dates[day] = dates.get(day, 0) + 1
            for lane_nodes in lanes.values():
                for index, node in enumerate(lane_nodes):
                    changes: dict[str, Any] = {}
                    flags = list(node.get("review_flags") or [])
                    if index and not node.get("predecessor_ids"):
                        changes["predecessor_ids"] = [lane_nodes[index - 1]["node_id"]]
                    if dates.get(str(node.get("occurred_at", ""))[:10], 0) > 1 and "关系待确认" not in flags:
                        flags.append("关系待确认")
                    successor = lane_nodes[index + 1] if index + 1 < len(lane_nodes) else None
                    if successor and node.get("status") != "已完成":
                        changes["status"] = "已完成"
                        changes["end_time"] = successor.get("start_time") or successor.get("occurred_at", "")
                    elif not successor and node.get("status") == "进行中":
                        summary = node.get("summary", "")
                        if any(word in summary for word in completion_words) or project.get("project_status") == "已完成":
                            changes["status"] = "已完成"
                            changes["end_time"] = node.get("end_time") or node.get("occurred_at", "")
                        else:
                            changes["status"] = "待确认"
                            if "状态待确认" not in flags:
                                flags.append("状态待确认")
                    changes["review_flags"] = flags
                    changes["review_state"] = "待确认" if flags else "已确认"
                    self._rewrite_node_meta(node, changes)
        _atomic_write(self.schema_path, _safe_yaml({
            "version": 2,
            "migrated_at": _now().isoformat(),
            "backup": backup_root.relative_to(self.vault_root).as_posix(),
        }) + "\n")
        for project in self.list_projects():
            self.sync_overview(project["project_id"])
        self.sync_native_views()
        return {"migrated": True, "version": 2, "backup": str(backup_root), "review": len(self.review_items())}

    def migrate_schema_v3(self) -> dict[str, Any]:
        current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) if self.schema_path.exists() else {}
        if int((current or {}).get("version", 1)) < 2:
            self.migrate_schema_v2()
            current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) or {}
        if int((current or {}).get("version", 2)) >= 3:
            return {"migrated": False, "version": 3}

        stamp = _now().strftime("%Y%m%d-%H%M%S")
        backup_root = self.backups_dir / f"schema-v2-{stamp}"
        for source in (self.projects_dir, self.tasks_dir, self.nodes_dir):
            if source.exists():
                shutil.copytree(source, backup_root / source.name)

        for project in list(self.list_projects()):
            classification = _classify_project(
                project["name"], project.get("major_work", ""), project.get("overview_path", "")
            )
            aliases = set(project.get("aliases") or [])
            self.update_project(
                project["project_id"],
                {**classification, "aliases": sorted(aliases)},
                sync_overview=False,
            )

        default_tasks: dict[str, str] = {}
        for project in self.list_projects():
            existing_tasks = self.list_tasks(project["project_id"])
            project_nodes = [item for item in self.list_nodes() if item.get("project_id") == project["project_id"]]
            latest = project_nodes[0] if project_nodes else None
            if existing_tasks:
                default_tasks[project["project_id"]] = existing_tasks[-1]["task_id"]
                continue
            if project.get("project_status") == "已完成":
                task_status = "已完成"
            elif project.get("work_type") == "单次事项" and latest and latest.get("status") == "已完成":
                task_status = "已完成"
            elif latest and latest.get("status") in {"等待中", "阻塞", "待确认"}:
                task_status = latest["status"]
            else:
                task_status = "进行中"
            deterministic_id = f"task-history-{hashlib.sha1(project['project_id'].encode('utf-8')).hexdigest()[:12]}"
            task = self.create_task(
                project=project["project_id"],
                title=project["name"] if project.get("work_type") == "单次事项" else f"{project['name']}主线",
                status=task_status,
                description="由历史工作节点建立的默认主线；后续新增任务时可继续拆分。",
                required=project.get("work_type") == "项目",
                source="Obsidian历史",
                task_id=deterministic_id,
                derived_from_history=True,
            )
            if latest:
                task = self.update_task(task["task_id"], {"latest_node_id": latest["node_id"]}, sync_project=False)
            default_tasks[project["project_id"]] = task["task_id"]

        updated_nodes = 0
        projects_by_id = {item["project_id"]: item for item in self.list_projects()}
        for node in self.list_nodes(effective_only=False):
            project = projects_by_id.get(node.get("project_id"))
            if not project:
                continue
            changes = {
                "work_domain": project.get("work_domain", ""),
                "work_category": project.get("work_category", ""),
                "work_type": project.get("work_type", "项目"),
                "view_tags": project.get("view_tags", []),
                "task_id": node.get("task_id") or default_tasks.get(project["project_id"], ""),
                "intent_type": node.get("intent_type") or "",
            }
            if any(node.get(key) != value for key, value in changes.items()):
                self._rewrite_node_meta(node, changes)
                updated_nodes += 1
        self._invalidate_node_cache()

        _atomic_write(self.schema_path, _safe_yaml({
            "version": 3,
            "migrated_at": _now().isoformat(),
            "backup": backup_root.relative_to(self.vault_root).as_posix(),
            "model": "personal-work-panorama",
        }) + "\n")
        for project in self.list_projects():
            self.sync_overview(project["project_id"])
        self.sync_native_views()
        self.write_work_panorama_pages()
        return {
            "migrated": True,
            "version": 3,
            "backup": str(backup_root),
            "projects": len(self.list_projects()),
            "tasks": len(self.list_tasks()),
            "nodes": len(self.list_nodes(effective_only=False)),
            "updated_nodes": updated_nodes,
        }

    def migrate_schema_v4(self) -> dict[str, Any]:
        current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) if self.schema_path.exists() else {}
        if int((current or {}).get("version", 1)) < 3:
            self.migrate_schema_v3()
            current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) or {}
        if int((current or {}).get("version", 3)) >= 4:
            return {"migrated": False, "version": 4}

        stamp = _now().strftime("%Y%m%d-%H%M%S")
        backup_root = self.backups_dir / f"schema-v3-{stamp}"
        shutil.copytree(self.nodes_dir, backup_root / self.nodes_dir.name)
        updated = 0
        for node in self.list_nodes(effective_only=False):
            changes = {
                "summary": node.get("summary", ""),
                "next_action": node.get("next_action", ""),
                "related_path": node.get("related_path", ""),
            }
            path = Path(node["path"])
            meta, _ = _parse_frontmatter(path)
            if any(meta.get(key, "") != value for key, value in changes.items()):
                self._rewrite_node_meta(node, changes)
                updated += 1
        self._invalidate_node_cache()
        _atomic_write(self.schema_path, _safe_yaml({
            "version": 4,
            "migrated_at": _now().isoformat(),
            "backup": backup_root.relative_to(self.vault_root).as_posix(),
            "model": "personal-work-panorama",
            "view_properties": ["summary", "next_action", "related_path"],
        }) + "\n")
        self.write_work_panorama_pages()
        return {"migrated": True, "version": 4, "backup": str(backup_root), "updated_nodes": updated}

    def migrate_schema_v5(self) -> dict[str, Any]:
        current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) if self.schema_path.exists() else {}
        if int((current or {}).get("version", 1)) < 4:
            self.migrate_schema_v4()
            current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) or {}
        if int((current or {}).get("version", 4)) >= 5:
            return {"migrated": False, "version": 5}
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        backup_root = self.backups_dir / f"schema-v4-{stamp}"
        for source in (self.projects_dir, self.tasks_dir, self.nodes_dir):
            if source.exists():
                shutil.copytree(source, backup_root / source.name)
        _atomic_write(self.schema_path, _safe_yaml({
            "version": 5,
            "migrated_at": _now().isoformat(),
            "backup": backup_root.relative_to(self.vault_root).as_posix(),
            "model": "personal-work-panorama-with-project-learning",
            "experience": ["流程", "问题与决策", "产出", "复盘", "方法论候选", "方法论库"],
        }) + "\n")
        return {"migrated": True, "version": 5, "backup": str(backup_root)}

    def migrate_schema_v6(self) -> dict[str, Any]:
        """Keep stable IDs in frontmatter while replacing opaque file names with readable names."""
        current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) if self.schema_path.exists() else {}
        if int((current or {}).get("version", 1)) < 5:
            self.migrate_schema_v5()
            current = yaml.safe_load(self.schema_path.read_text(encoding="utf-8")) or {}
        if int((current or {}).get("version", 5)) >= 6:
            return {"migrated": False, "version": 6}

        stamp = _now().strftime("%Y%m%d-%H%M%S")
        backup_root = self.backups_dir / f"schema-v5-{stamp}"
        for source in (self.projects_dir, self.tasks_dir, self.nodes_dir, self.canvas_dir, self.views_dir):
            if source.exists():
                shutil.copytree(source, backup_root / source.name)

        def move_readable(source: Path, target: Path, suffix: str) -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source == target:
                return target
            if target.exists():
                target = target.with_name(f"{target.stem}—{suffix}{target.suffix}")
            os.replace(source, target)
            return target

        project_items = self.list_projects()
        project_map = {item["project_id"]: item for item in project_items}
        counters = {"projects": 0, "tasks": 0, "nodes": 0, "canvases": 0}
        for project in project_items:
            source = Path(project["path"])
            target = self._project_path(project["name"])
            if source != target:
                move_readable(source, target, project["project_id"][-6:])
                counters["projects"] += 1

        self._project_cache.clear()
        task_paths = list(self.tasks_dir.rglob("*.md"))
        for source in task_paths:
            meta, _ = _parse_frontmatter(source)
            if meta.get("type") != "work-task":
                continue
            project = project_map.get(meta.get("project_id", ""))
            project_name = project["name"] if project else meta.get("project_name", "未归类项目")
            target = self._task_path(project_name, str(meta.get("title") or "未命名任务"))
            if source != target:
                move_readable(source, target, str(meta.get("task_id") or "task")[-6:])
                counters["tasks"] += 1
        for folder in sorted((item for item in self.tasks_dir.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass

        self._task_cache.clear()
        node_paths = list(self.nodes_dir.rglob("*.md"))
        for source in node_paths:
            meta, body = _parse_frontmatter(source)
            if meta.get("type") != "weekly-node":
                continue
            occurred_raw = str(meta.get("occurred_at") or _now().isoformat())
            try:
                occurred = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                occurred = _now()
            project = project_map.get(meta.get("project_id", ""))
            project_name = project["name"] if project else meta.get("project_name", "未归类项目")
            summary = _extract_section(body, "本次进展") or str(meta.get("summary") or "工作记录")
            target = self._node_path(occurred, str(meta.get("node_id") or "node"), project_name, summary)
            if source != target:
                move_readable(source, target, str(meta.get("node_id") or "node")[-6:])
                counters["nodes"] += 1
        self._invalidate_node_cache()

        for source in list(self.canvas_dir.glob("*.canvas")):
            project_id = source.stem.removesuffix("-工作关系")
            project = project_map.get(project_id)
            if not project:
                continue
            target = self._canvas_path(project["name"])
            if source != target:
                move_readable(source, target, project_id[-6:])
                counters["canvases"] += 1

        _atomic_write(self.schema_path, _safe_yaml({
            "version": 6,
            "migrated_at": _now().isoformat(),
            "backup": backup_root.relative_to(self.vault_root).as_posix(),
            "model": "personal-work-panorama-readable-storage",
            "internal_ids": "frontmatter only",
            "human_readable_storage": ["项目台账", "任务台账", "工作节点", "项目关系图"],
        }) + "\n")
        self.sync_native_views()
        self.write_work_panorama_pages()
        return {"migrated": True, "version": 6, "backup": str(backup_root), **counters}

    def normalize_existing_projects(self) -> dict[str, Any]:
        """Add the consistent project-experience shell only to projects that already have an overview."""
        full_projects: list[dict[str, Any]] = []
        light_projects: list[dict[str, Any]] = []
        for project in self.list_projects():
            if not project.get("display_in_views", True):
                light_projects.append(project)
                continue
            overview = self._overview_file(project)
            if overview and overview.exists():
                self.initialize_project_experience(project["project_id"])
                self.sync_overview(project["project_id"])
                full_projects.append(project)
            else:
                light_projects.append(project)
        self.sync_native_views()
        lines = [
            "# 个人工作结构说明", "",
            "## 完整项目", "",
            "以下项目已有项目总览，统一采用“总览—流程—产出—问题与决策—复盘”结构。", "",
            *( [f"- {self._wikilink_for_project(item)}（{item.get('project_status', '进行中')}）" for item in sorted(full_projects, key=lambda value: value["name"])] or ["- 暂无"] ), "",
            "## 轻量工作记录", "",
            f"其余 {len(light_projects)} 个事项只有任务或工作节点，保留在工作全景和任务台账，不创建空项目目录。", "",
            "- 当出现明确项目背景、流程、产出或原始资料目录时，再升级为完整项目。",
            "- 单次事项和持续工作继续通过日历节点、任务台账和周报呈现。", "",
        ]
        path = self.panorama_dir / "工作结构说明.md"
        _atomic_write(path, "\n".join(lines))
        self.write_work_panorama_pages()
        return {"full_projects": len(full_projects), "light_projects": len(light_projects), "guide_path": str(path)}

    def humanize_legacy_storage(self) -> dict[str, Any]:
        """Sweep merged ledgers and audit-only nodes that are intentionally excluded from normal lists."""
        opaque_files = [
            path for folder in (self.projects_dir, self.tasks_dir, self.nodes_dir, self.canvas_dir)
            for path in folder.rglob("*.md") if path.name.startswith(("prj-", "task-", "node-"))
        ]
        opaque_files.extend(path for path in self.canvas_dir.glob("prj-*.canvas"))
        if not opaque_files:
            return {"migrated": False, "backup": "", "projects": 0, "tasks": 0, "nodes": 0, "canvases": 0}
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        backup_root = self.backups_dir / f"readable-name-sweep-{stamp}"
        for source in (self.projects_dir, self.tasks_dir, self.nodes_dir, self.canvas_dir):
            if source.exists():
                shutil.copytree(source, backup_root / source.name)

        def move_readable(source: Path, target: Path, suffix: str) -> bool:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source == target:
                return False
            if target.exists():
                target = target.with_name(f"{target.stem}—{suffix}{target.suffix}")
            os.replace(source, target)
            return True

        all_project_meta: dict[str, dict[str, Any]] = {}
        counters = {"projects": 0, "tasks": 0, "nodes": 0, "canvases": 0}
        for source in list(self.projects_dir.glob("*.md")):
            meta, _ = _parse_frontmatter(source)
            if meta.get("type") not in {"weekly-project", "weekly-project-merged"}:
                continue
            project_id = str(meta.get("project_id") or "")
            if project_id:
                all_project_meta[project_id] = meta
            title = str(meta.get("name") or "未命名项目")
            if meta.get("type") == "weekly-project-merged":
                title += "-已合并"
            target = self.projects_dir / f"{_safe_filename(title, '已合并项目')}.md"
            if move_readable(source, target, project_id[-6:] or "merged"):
                counters["projects"] += 1

        for source in list(self.tasks_dir.rglob("*.md")):
            meta, _ = _parse_frontmatter(source)
            if meta.get("type") != "work-task":
                continue
            project_meta = all_project_meta.get(str(meta.get("project_id") or ""), {})
            project_name = str(project_meta.get("name") or meta.get("project_name") or "未归类项目")
            target = self._task_path(project_name, str(meta.get("title") or "未命名任务"))
            if move_readable(source, target, str(meta.get("task_id") or "task")[-6:]):
                counters["tasks"] += 1
        for folder in sorted((item for item in self.tasks_dir.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass

        for source in list(self.nodes_dir.rglob("*.md")):
            meta, body = _parse_frontmatter(source)
            if meta.get("type") not in {"weekly-node", "weekly-node-duplicate"}:
                continue
            occurred_raw = str(meta.get("occurred_at") or _now().isoformat())
            try:
                occurred = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                occurred = _now()
            project_meta = all_project_meta.get(str(meta.get("project_id") or ""), {})
            project_name = str(project_meta.get("name") or meta.get("project_name") or "未归类项目")
            summary = _extract_section(body, "本次进展") or str(meta.get("summary") or "工作记录")
            target = self._node_path(occurred, str(meta.get("node_id") or "node"), project_name, summary)
            if move_readable(source, target, str(meta.get("node_id") or "node")[-6:]):
                counters["nodes"] += 1

        for source in list(self.canvas_dir.glob("prj-*.canvas")):
            project_id = source.stem.removesuffix("-工作关系")
            project_meta = all_project_meta.get(project_id)
            if not project_meta:
                continue
            if move_readable(source, self._canvas_path(str(project_meta.get("name") or "未命名项目")), project_id[-6:]):
                counters["canvases"] += 1
        self._project_cache.clear()
        self._task_cache.clear()
        self._invalidate_node_cache()
        self.sync_native_views()
        self.write_work_panorama_pages()
        return {"migrated": True, "backup": str(backup_root), **counters}

    def _overview_file(self, project: dict[str, Any]) -> Path | None:
        raw = project.get("overview_path") or ""
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else self.vault_root / path

    @_timed_method
    def sync_overview(self, project_id: str) -> None:
        project = self.get_project(project_id)
        if not project:
            return
        overview = self._overview_file(project)
        if not overview or not overview.exists():
            return
        nodes = self.list_nodes(project_id=project["project_id"])[:10]
        tasks = self.list_tasks(project["project_id"])
        open_tasks = [item for item in tasks if item.get("status") != "已完成"]
        issues = self.list_issues(project["project_id"])
        open_issues = [item for item in issues if item.get("status") not in {"已解决", "已接受"}]
        latest = nodes[0] if nodes else None
        table_rows = []
        for item in nodes:
            stamp = str(item.get("occurred_at", ""))[:16].replace("T", " ")
            if item.get("time_precision") == "date":
                stamp = stamp[:10]
            table_rows.append(
                f"| {stamp} | {_clean_cell(item.get('kind', ''))} | {_clean_cell(item.get('status', ''))} | {_clean_cell(item.get('summary', ''))} |"
            )
        managed = [
            MANAGED_START,
            "## 周报系统同步",
            "",
            "> 当前状态、进展和下一步以本区块为准；区块外正文保留项目背景与历史说明。",
            "",
            f"- **项目状态**：{project.get('project_status', '进行中')}",
            f"- **最新进度**：{_clean_cell(latest.get('summary', '暂无记录')) if latest else '暂无记录'}",
            f"- **下一步动作**：{_clean_cell(latest.get('next_action', '无')) if latest and latest.get('next_action') else '无'}",
            f"- **最近更新**：{str(latest.get('occurred_at', ''))[:16].replace('T', ' ') if latest else '暂无记录'}",
            f"- **工作关系图**：[[{self._canvas_path(project['name']).relative_to(self.vault_root).with_suffix('').as_posix()}|打开主线与支线图]]",
            "",
            "### 当前任务",
            "",
            *( [f"- `{item.get('status', '待开始')}` {item.get('title', '')}" for item in open_tasks[:10]] or ["- 暂无未完成任务"] ),
            "",
            "### 最近工作时间线",
            "",
            "| 时间 | 类型 | 状态 | 进展 |",
            "|---|---|---|---|",
            *(table_rows or ["| - | - | - | 暂无记录 |"]),
            "",
            "### 项目经验",
            "",
            f"- **待处理问题**：{len(open_issues)} 个",
            *( [f"- `{item.get('status', '待分析')}` [[{Path(item['path']).relative_to(self.vault_root).with_suffix('').as_posix()}|{Path(item['path']).stem}]]" for item in open_issues[:5]] or ["- 暂无待处理问题"] ),
            f"- **经验索引**：[[{self._experience_index_path(project).relative_to(self.vault_root).with_suffix('').as_posix()}|打开流程、问题、产出与复盘]]" if self._experience_index_path(project).exists() else "- **经验索引**：尚未建立",
            "",
            "> 此区块由周报系统维护，请在网页看板中编辑工作节点。",
            MANAGED_END,
        ]
        replacement = "\n".join(managed)
        text = overview.read_text(encoding="utf-8")
        original_text = text
        text = _separate_domain_block(text)
        frontmatter = re.match(r"^---\n(.*?)\n---", text, re.S)
        if frontmatter:
            header = frontmatter.group(1)
            desired_status = str(project.get("project_status") or "进行中")
            if re.search(r"(?m)^状态:\s*.*$", header):
                header = re.sub(r"(?m)^状态:\s*.*$", f"状态: {desired_status}", header)
            else:
                header = header.rstrip() + f"\n状态: {desired_status}"
            text = f"---\n{header}\n---" + text[frontmatter.end():]
        else:
            text = "---\n" + _safe_yaml({"项目名称": project["name"], "状态": project.get("project_status") or "进行中"}) + "\n---\n\n" + text
        pattern = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), re.S)
        updated = pattern.sub(replacement, text) if pattern.search(text) else text.rstrip() + "\n\n" + replacement + "\n"
        if updated == original_text:
            return
        backup = self.backups_dir / overview.relative_to(self.vault_root)
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(overview, backup)
        _atomic_write(overview, updated)

    def load_calendar(self) -> dict[str, list[str]]:
        raw = yaml.safe_load(self.calendar_path.read_text(encoding="utf-8")) or {}
        return {
            "holidays": sorted({str(item) for item in raw.get("holidays", [])}),
            "workdays": sorted({str(item) for item in raw.get("workdays", [])}),
        }

    def save_calendar(self, holidays: Iterable[str], workdays: Iterable[str]) -> dict[str, list[str]]:
        payload = {
            "holidays": sorted({date.fromisoformat(item).isoformat() for item in holidays if item}),
            "workdays": sorted({date.fromisoformat(item).isoformat() for item in workdays if item}),
        }
        _atomic_write(
            self.calendar_path,
            "# 默认周一至周五。节假日和调休日期可在网页中维护。\n" + _safe_yaml(payload) + "\n",
        )
        return payload

    def week_context(self, anchor: str | date | None = None) -> WeekContext:
        current = date.fromisoformat(anchor) if isinstance(anchor, str) else (anchor or date.today())
        monday = current - timedelta(days=current.weekday())
        sunday = monday + timedelta(days=6)
        calendar = self.load_calendar()
        holidays = {date.fromisoformat(item) for item in calendar["holidays"]}
        extra = {date.fromisoformat(item) for item in calendar["workdays"]}
        candidates = [monday + timedelta(days=offset) for offset in range(7)]
        workdays = tuple(day for day in candidates if day in extra or (day.weekday() < 5 and day not in holidays))
        if not workdays:
            workdays = (monday, monday + timedelta(days=4))
        return WeekContext(current, monday, sunday, workdays[0], workdays[-1], workdays)

    def version_token(self) -> str:
        records: list[str] = []
        for folder in (self.projects_dir, self.tasks_dir, self.nodes_dir):
            for path in folder.rglob("*.md"):
                stat = path.stat()
                records.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        return hashlib.sha1("|".join(sorted(records)).encode("utf-8")).hexdigest()

    def dashboard_state(self, start: str | date | None = None, end: str | date | None = None) -> dict[str, Any]:
        today = date.today()
        range_start = date.fromisoformat(start) if isinstance(start, str) and start else (start or date(today.year, 1, 1))
        range_end = date.fromisoformat(end) if isinstance(end, str) and end else (end or today)
        if range_start > range_end:
            raise WorklogError("开始日期不能晚于结束日期")
        week = self.week_context(today)
        nodes = self.list_nodes()
        history_nodes = [item for item in self.list_nodes(effective_only=False) if not item.get("excluded")]
        effective_ids = {item["node_id"] for item in nodes}
        for item in history_nodes:
            item["is_effective"] = item["node_id"] in effective_ids
        projects = self.list_projects()
        tasks = self.list_tasks()
        tasks_by_project: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            tasks_by_project.setdefault(task["project_id"], []).append(task)
        by_project: dict[str, list[dict[str, Any]]] = {}
        for item in nodes:
            by_project.setdefault(item["project_id"], []).append(item)
        summaries = []
        for project in projects:
            project_nodes = by_project.get(project["project_id"], [])
            latest = project_nodes[0] if project_nodes else None
            visible_nodes: list[dict[str, Any]] = []
            lane_map: dict[str, dict[str, Any]] = {}
            graph_dates: list[str] = []
            for node in project_nodes:
                node_start = date.fromisoformat(str(node.get("start_time") or node.get("occurred_at"))[:10])
                raw_end = node.get("end_time")
                node_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else (today if node.get("status") != "已完成" else node_start)
                if node_end < range_start or node_start > range_end:
                    continue
                visible_nodes.append(node)
                graph_dates.extend((node_start.isoformat(), max(node_start, node_end).isoformat()))
                lane = lane_map.setdefault(node.get("lane_id", "main"), {
                    "lane_id": node.get("lane_id", "main"),
                    "lane_name": node.get("lane_name", "主线"),
                    "track_type": node.get("track_type", "主线"),
                    "nodes": [],
                })
                lane["nodes"].append(node["node_id"])
            lanes = sorted(lane_map.values(), key=lambda item: (item["track_type"] != "主线", item["lane_name"]))
            summaries.append({
                **{key: project.get(key) for key in (
                    "project_id", "name", "major_work", "key_point", "project_status", "overview_path", "canvas_path",
                    "work_domain", "work_category", "work_type", "project_mode", "view_tags",
                )},
                "latest": latest,
                "tasks": tasks_by_project.get(project["project_id"], []),
                "open_task_count": sum(item.get("status") != "已完成" for item in tasks_by_project.get(project["project_id"], [])),
                "node_count": len(project_nodes),
                "range_node_count": len(visible_nodes),
                "range_start": min(graph_dates) if graph_dates else "",
                "range_end": max(graph_dates) if graph_dates else "",
                "lanes": lanes,
                "review_count": sum(item.get("review_state") == "待确认" for item in visible_nodes),
            })
        summaries.sort(key=lambda item: (item["latest"] or {}).get("occurred_at", ""), reverse=True)
        return {
            "week": week.as_dict(),
            "projects": summaries,
            "nodes": nodes,
            "tasks": tasks,
            "history_nodes": history_nodes,
            "calendar": self.load_calendar(),
            "range": {"start": range_start.isoformat(), "end": range_end.isoformat()},
            "review_count": len(self.review_items()),
            "version": self.version_token(),
        }
