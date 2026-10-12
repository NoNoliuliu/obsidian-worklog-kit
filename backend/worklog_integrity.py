"""Integrity audit and controlled repair."""
from __future__ import annotations

from worklog_base import (  # noqa: F401
    Any, Counter, MANAGED_END, MANAGED_START, Path, _atomic_write, _clean_cell, _file_hash, _now,
    _parse_frontmatter, _safe_yaml, completed_project_findings, copy, datetime, defaultdict, hashlib,
    re, shutil
)


class IntegrityMixin:
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
