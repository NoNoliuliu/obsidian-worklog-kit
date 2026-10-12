"""Worklog store: projects, tasks, nodes and records. Feature areas live in mixin modules."""
from __future__ import annotations

from worklog_base import *  # noqa: F401,F403 - public names re-exported for callers
from worklog_base import (  # noqa: F401
    Any, Callable, ConflictError, DOMAIN_END, DOMAIN_START, Iterable, MANAGED_END, MANAGED_START,
    NODE_INDEX_VERSION, Path, UnknownProjectError, VALID_INTENT_TYPES, VALID_KINDS, VALID_PROJECT_MODES,
    VALID_PROJECT_STATUSES, VALID_RELATION_TYPES, VALID_STATUSES, VALID_TRACK_TYPES, VALID_WORK_TYPES,
    WeekContext, WorklogError, _INTENT_WRITE_STORE, _as_bool, _atomic_write, _classify_project,
    _extract_section, _file_hash, _normalize, _now, _parse_frontmatter, _parse_frontmatter_text,
    _safe_filename, _safe_yaml, _search_normalize, _timed_method, _write_if_changed, audit_structure,
    completed_project_findings, contextmanager, copy, date, datetime, default_project_parent,
    defaultdict, fcntl, finding, hashlib, json, msvcrt, normalize_source, os,
    project_structure_findings, re, shutil, threading, time, timedelta, uuid, yaml
)
from worklog_materials import MaterialsMixin
from worklog_knowledge import KnowledgeMixin
from worklog_search import SearchMixin
from worklog_views import ViewsMixin
from worklog_integrity import IntegrityMixin
from worklog_migrations import MigrationsMixin


class WorklogStore(MaterialsMixin, KnowledgeMixin, SearchMixin, ViewsMixin, IntegrityMixin, MigrationsMixin):
    def __init__(
        self, vault_root: str | Path, *, read_only: bool = False,
        node_index_path: str | Path | None = None,
        entity_index_path: str | Path | None = None,
        backups_dir: str | Path | None = None,
    ):
        self.vault_root = Path(vault_root)
        self.root = self.vault_root / "周报系统"
        self.projects_dir = self.root / "项目台账"
        self.tasks_dir = self.root / "任务台账"
        self.nodes_dir = self.root / "工作节点"
        self.drafts_dir = self.root / "周报草稿"
        # Backups default to the vault; config.yaml backups_dir can keep them outside so Obsidian does not index them.
        self.backups_dir = Path(backups_dir).expanduser() if backups_dir else self.root / "备份"
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

    def _backup_ref(self, path: Path) -> str:
        """Vault-relative backup path when inside the vault, absolute otherwise (backups_dir may be outside)."""
        try:
            return Path(path).relative_to(self.vault_root).as_posix()
        except ValueError:
            return str(path)

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
        # Nodes no longer carry key_point/major_work, so those edits do not rewrite every node of the project.
        structural_change = any(
            key in changes and changes[key] != project.get(key)
            for key in ("name", *NODE_PROJECT_FIELDS)
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
                meta.update(node_project_fields(project, meta))
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
        content = f"---\n{_safe_yaml(meta)}\n---\n\n{body.lstrip()}"
        self._write_existing_node_without_replace(path, content)
        self._refresh_node_cache_entry(path)

    def _write_existing_node_without_replace(self, path: Path, content: str) -> None:
        """Rewrite a node in place so sandbox monitors do not see a delete.

        Keep a durable preimage because an in-place write is not atomic. The
        caller holds the Vault mutation lock, so another worklog writer cannot
        observe a partly written node through this backend.
        """
        original = path.read_bytes()
        updated = content.encode("utf-8")
        if original == updated:
            return
        path_key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        backup = self.backups_dir / "node-meta-rewrite" / f"{path_key}-{uuid.uuid4().hex}.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(updated)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(original)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            raise

    def _is_managed_intent_path(self, path: Path) -> bool:
        """Limit non-replacing writes to regular files inside this Vault."""
        resolved = path.resolve()
        return (path.is_file() and not path.is_symlink()
                and resolved.is_relative_to(self.vault_root.resolve())
                and not resolved.is_relative_to(self.backups_dir.resolve()))

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
            # Microseconds keep retries written in the same second distinguishable (undo --last).
            created_at = datetime.now().isoformat(timespec="microseconds")
            flags = list(dict.fromkeys(str(item) for item in (review_flags or []) if str(item)))
            if status == "待确认" and "状态待确认" not in flags:
                flags.append("状态待确认")
            if flags:
                review_state = "待确认"
            meta = {
                "type": "weekly-node",
                "node_id": node_id,
                "project_id": project_meta["project_id"],
                **node_project_fields(project_meta),
                "occurred_at": occurred.isoformat(),
                "time_precision": time_precision,
                "kind": kind,
                "status": status,
                "source": source,
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
            meta.update(node_project_fields(canonical_project, meta))
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
                **node_project_fields(target, meta),
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
            token = _INTENT_WRITE_STORE.set(self)
            try:
                return self._record_intent(payload)
            finally:
                _INTENT_WRITE_STORE.reset(token)

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
                source=normalize_source(payload.get("source")),
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
            source=normalize_source(payload.get("source")),
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
