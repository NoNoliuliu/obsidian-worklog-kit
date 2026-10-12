"""Derived views: project graph, Canvas, Bases, panorama pages and overview sync."""
from __future__ import annotations

from worklog_base import (  # noqa: F401
    Any, DAILY_CATEGORIES, DOMAIN_END, DOMAIN_START, Iterable, MANAGED_END, MANAGED_START, Path,
    UnknownProjectError, TRAINING_CATEGORIES, WorklogError, _atomic_write, _clean_cell, _parse_frontmatter,
    _safe_filename, _safe_yaml, _separate_domain_block, _timed_method, _write_if_changed, date, hashlib,
    json, re, shutil, time
)


class ViewsMixin:
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
