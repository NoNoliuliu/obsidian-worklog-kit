"""Project folders, experience scaffolding and material indexes."""
from __future__ import annotations

from worklog_base import (  # noqa: F401
    Any, Iterable, MATERIAL_END, MATERIAL_START, Path, UnknownProjectError, WorklogError, _atomic_write,
    _clean_cell, _file_hash, _now, _safe_yaml, date, defaultdict, re
)


class MaterialsMixin:
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
