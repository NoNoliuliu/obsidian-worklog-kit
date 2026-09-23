"""Read-only structural diagnostics; findings never authorize a move or repair."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any


TRAINING_CATEGORIES = ("平台与数据", "培训实施", "技术与AI分享", "考试竞赛", "部门需求支持", "日常运营")
DAILY_CATEGORIES = ("培训资源规划", "组织发展支持", "内容与传播", "临时支持")
WORK_DOMAINS = ("项目管理", "培训工作", "体系评审", "日常与专项工作")


def default_project_parent(domain: str, category: str) -> Path:
    if domain == "培训工作":
        number = TRAINING_CATEGORIES.index(category) + 1 if category in TRAINING_CATEGORIES else 6
        return Path(domain) / f"{number:02d}-{category}"
    if domain == "日常与专项工作":
        return Path(domain) / category
    return Path("体系评审" if domain == "体系评审" else "项目管理")


def overview_file(vault: Path, project: dict[str, Any]) -> Path | None:
    raw = str(project.get("overview_path") or "").replace("\\", "/").strip()
    if not raw:
        return None
    path = Path(raw)
    return (path if path.is_absolute() else vault / path).resolve()


def finding(code: str, project: dict[str, Any], message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "project_id": project.get("project_id"),
            "project": project.get("name"), "message": message, **details}


def project_structure_findings(vault: Path, project: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    domain, category = project.get("work_domain", ""), project.get("work_category", "")
    indexed = {"培训工作": TRAINING_CATEGORIES, "日常与专项工作": DAILY_CATEGORIES}
    if domain not in WORK_DOMAINS:
        results.append(finding("unknown_domain", project, "领域不在当前工作全景导航中，请核对是否为自定义领域。", domain=domain))
    elif domain in indexed and category not in indexed[domain]:
        results.append(finding("unindexed_category", project, "分类不在该领域的分类索引中，请核对分类。", category=category))
    path = overview_file(vault, project)
    if path is None:
        return results
    if not path.is_file():
        results.append(finding("overview_missing", project, "总览路径未指向现存文件；更新路径不会搬移原文件。", path=str(path)))
    if not path.is_relative_to(vault.resolve()):
        results.append(finding("external_overview", project, "总览位于 Vault 外，需核对自定义路径；不会自动搬移。", path=str(path)))
    elif domain in WORK_DOMAINS and not (domain in indexed and category not in indexed[domain]):
        expected = (vault / default_project_parent(domain, category)).resolve()
        # Domain navigation pages and numbered daily indexes are legitimate views.
        navigation = path.name.startswith("00-") and path.parent in {
            (vault / domain).resolve(), expected,
            *((vault / domain / f"{i:02d}-{c}").resolve() for i, c in enumerate(indexed.get(domain, ()), 1)),
        }
        if not navigation and not path.is_relative_to(expected):
            results.append(finding("category_path_deviation", project,
                                   "总览目录偏离默认分类位置；可能是自定义路径，需核对，不自动判定为错位。",
                                   path=str(path), expected_parent=str(expected)))
    return results


def completed_project_findings(project: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if project.get("project_status") != "已完成":
        return []
    open_tasks = [t for t in tasks if t.get("project_id") == project.get("project_id") and t.get("status") != "已完成"]
    if not open_tasks:
        return []
    return [finding("completed_project_open_tasks", project,
                    "已完成项目仍有未完成任务；请区分带遗留结项、历史补录和新一轮工作，不自动重启。",
                    project_mode=project.get("project_mode"), open_task_count=len(open_tasks),
                    tasks=[{"task_id": t.get("task_id"), "title": t.get("title"), "status": t.get("status")}
                           for t in open_tasks])]


def audit_structure(store: Any) -> dict[str, Any]:
    projects = store.list_projects()
    errors: dict[str, list[Any]] = defaultdict(list)
    warnings: dict[str, list[Any]] = defaultdict(list)
    empty = []
    overview_owners: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    directory_owners: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    canvases = set()
    for project in projects:
        for item in project_structure_findings(store.vault_root, project):
            (errors if item["code"] == "overview_missing" else warnings)[item["code"]].append(item)
        path = overview_file(store.vault_root, project)
        if path is None:
            empty.append(project["name"])
        else:
            overview_owners[path].append(project)
            directory_owners[path.parent].append(project)
        canvases.add(store._canvas_path(project["name"]).resolve())
        if project.get("canvas_path"):
            raw = Path(project["canvas_path"])
            canvases.add((raw if raw.is_absolute() else store.vault_root / raw).resolve())
        ledger = Path(project["path"])
        if ledger != store._project_path(project["name"]):
            warnings["ledger_name_deviation"].append(finding(
                "ledger_name_deviation", project, "项目显示名称与台账文件名不同；关联仍以稳定 ID 为准。", path=str(ledger)))
    for code, owners, bucket, message in (
        ("duplicate_overview", overview_owners, errors, "多个项目指向同一个总览，写入受控区可能相互覆盖。"),
        ("shared_project_directory", directory_owners, warnings, "多个项目共用目录，不能将整个目录作为单项目搬移。"),
    ):
        for path, items in sorted(owners.items()):
            if len(items) > 1:
                bucket[code].append({"code": code, "path": str(path), "projects": [p["name"] for p in items], "message": message})
    for path in sorted(store.canvas_dir.glob("*.canvas")):
        if path.resolve() not in canvases:
            warnings["unlinked_canvas"].append({"code": "unlinked_canvas", "path": str(path),
                                                "message": "关系图未被当前正式项目关联，可能是历史或手工文件；只提示，不移动或删除。"})
    # Scan overview candidates only in business domains, not backups or report snapshots.
    for domain in WORK_DOMAINS:
        root = store.vault_root / domain
        for path in sorted(root.rglob("*项目总览*.md")):
            if any(part in {"备份", "周报草稿", ".obsidian"} for part in path.relative_to(root).parts):
                continue
            if path.resolve() not in overview_owners:
                warnings["unlinked_overview"].append({"code": "unlinked_overview", "path": str(path),
                                                     "message": "总览候选文件未被当前正式项目关联，可能是历史或手工资料；不视为垃圾。"})
    return {"ok": not errors, "counts": {"projects": len(projects), "empty_overviews": len(empty)},
            "errors": dict(errors), "warnings": dict(warnings), "skipped_empty_overviews": empty,
            "scope": "项目路径、分类索引覆盖、总览占用及业务目录内总览候选、关系图关联；不校验任意正文引用或业务事实。"}
