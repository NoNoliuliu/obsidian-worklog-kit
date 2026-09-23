from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from worklog_core import (
    WorklogError,
    WorklogStore,
    _atomic_write,
    _parse_frontmatter,
    _safe_filename,
    _safe_yaml,
)


WORKFLOW_SECTION_FILES = {
    "overview": "00-工作流总览.md",
    "goals": "01-适用场景与目标.md",
    "roles": "02-人与AI职责分工.md",
    "recording_rules": "03-记录与分类规则.md",
    "experience_rules": "04-项目流程与经验沉淀规则.md",
    "reporting_rules": "05-周报生成规则.md",
    "ai_integrations": "06-多AI接入说明.md",
    "issues": "07-问题与优化记录.md",
    "changelog": "08-版本变更记录.md",
}

WORKFLOW_SECTION_TITLES = {
    key: Path(filename).stem.split("-", 1)[-1]
    for key, filename in WORKFLOW_SECTION_FILES.items()
}


def _snapshot_changed_file(path: Path, version_dir: Path, stamp: str) -> str:
    version_dir.mkdir(parents=True, exist_ok=True)
    snapshot = version_dir / f"{stamp}-{path.name}"
    _atomic_write(snapshot, path.read_text(encoding="utf-8"))
    return str(snapshot)


def _sync_workflow_index(store: WorklogStore) -> None:
    index_path = store.method_dir / "00-方法论库索引.md"
    start_marker = "<!-- work-mechanism:start -->"
    end_marker = "<!-- work-mechanism:end -->"
    entries: list[str] = []
    root = store.method_dir / "个人工作管理"
    for path in sorted(root.glob("*/00-工作流总览.md")):
        meta, _ = _parse_frontmatter(path)
        if meta.get("type") != "work-mechanism":
            continue
        relative = path.relative_to(store.vault_root).with_suffix("")
        entries.append(
            f"- [[{relative.as_posix()}|{meta.get('name') or path.parent.name}]]"
            f"：{meta.get('summary') or '持续维护'}"
        )
    block = "\n".join([
        start_marker,
        "## 工作机制与个人工作基础设施",
        "",
        "> 该区域保存工作管理规则和AI协同工作流，不计入项目、任务、工作日历和周报。",
        "",
        *(entries or ["- 暂无已登记工作机制"]),
        end_marker,
    ])
    original = index_path.read_text(encoding="utf-8") if index_path.exists() else "# 方法论库索引\n"
    if start_marker in original and end_marker in original:
        prefix = original.split(start_marker, 1)[0].rstrip()
        suffix = original.split(end_marker, 1)[1].lstrip()
        rewritten = f"{prefix}\n\n{block}\n"
        if suffix:
            rewritten += f"\n{suffix}"
    else:
        rewritten = f"{original.rstrip()}\n\n{block}\n"
    _atomic_write(index_path, rewritten)


def record_workflow_asset(store: WorklogStore, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    version = str(payload.get("version") or "").strip()
    sections = payload.get("sections")
    if not name or not summary or not version:
        raise WorklogError("工作机制必须包含name、summary和version")
    if not isinstance(sections, dict) or not sections:
        raise WorklogError("工作机制必须包含sections")
    unknown = sorted(set(sections) - set(WORKFLOW_SECTION_FILES))
    if unknown:
        raise WorklogError(f"未知工作机制章节：{', '.join(unknown)}")

    folder = store.method_dir / "个人工作管理" / _safe_filename(name, "未命名工作机制")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    updated_at = datetime.now().replace(microsecond=0).isoformat()
    snapshots: list[str] = []
    written: list[str] = []

    for key, content in sections.items():
        path = folder / WORKFLOW_SECTION_FILES[key]
        title = WORKFLOW_SECTION_TITLES[key]
        text = str(content or "").strip()
        meta = {
            "type": "work-mechanism",
            "name": name,
            "section": key,
            "category": "个人工作管理",
            "work_type": "AI协同工作流",
            "status": str(payload.get("status") or "持续维护"),
            "version": version,
            "summary": summary,
            "include_in_report": False,
            "display_in_work_panorama": False,
            "count_as_project": False,
            "managed_by": "obsidian-worklog",
            "updated_at": updated_at,
        }
        rendered = f"---\n{_safe_yaml(meta)}\n---\n\n# {title}\n\n{text}\n"
        if path.exists() and path.read_text(encoding="utf-8") != rendered:
            snapshots.append(_snapshot_changed_file(path, folder / "版本记录" / "快照", stamp))
        _atomic_write(path, rendered)
        written.append(str(path))

    _sync_workflow_index(store)
    return {
        "name": name,
        "version": version,
        "type": "work-mechanism",
        "folder": str(folder),
        "files": written,
        "snapshots": snapshots,
        "included_in_report": False,
        "count_as_project": False,
    }
