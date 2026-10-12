"""Historical schema migrations and legacy storage normalisation."""
from __future__ import annotations

from worklog_base import (  # noqa: F401
    Any, Path, VALID_PROJECT_STATUSES, _as_bool, _atomic_write,
    _classify_project, _extract_section, _now, _parse_frontmatter, _safe_filename, _safe_yaml, datetime,
    hashlib, os, shutil, yaml
)


class MigrationsMixin:
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
            "backup": self._backup_ref(backup_root),
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
            "backup": self._backup_ref(backup_root),
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
            "backup": self._backup_ref(backup_root),
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
            "backup": self._backup_ref(backup_root),
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
            "backup": self._backup_ref(backup_root),
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
