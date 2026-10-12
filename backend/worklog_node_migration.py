"""Remove legacy project-level fields (key_point, major_work) from existing work nodes.

Plan → review → apply → (optional) rollback, all hash-checked:

    python worklog_node_migration.py plan --vault <vault> --output plan.json
    python worklog_node_migration.py apply --vault <vault> --plan plan.json --expected-plan-hash <sha256>
    python worklog_node_migration.py rollback --vault <vault> --backup <backup-dir>

Only the two frontmatter keys (and their continuation lines) are removed; every other
byte of the file is kept. ``apply`` backs up each original under 周报系统/备份/ first and
refuses to touch a file whose hash differs from the reviewed plan. ``rollback`` restores
a file only if it still has the post-migration hash.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

FIELDS = ("key_point", "major_work")
NODES_DIR = Path("周报系统") / "工作节点"
BACKUP_DIR = Path("周报系统") / "备份"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strip_fields(text: str, fields: tuple[str, ...] = FIELDS) -> tuple[str, list[str]]:
    """Drop top-level frontmatter keys and their indented continuation lines; return new text and removed lines."""
    if not text.startswith("---\n"):
        return text, []
    end = text.find("\n---", 4)
    if end == -1:
        return text, []
    kept: list[str] = []
    removed: list[str] = []
    dropping = False
    for line in text[4:end].split("\n"):
        if line and not line[0].isspace():
            dropping = line.split(":", 1)[0] in fields
        elif not line.strip():
            dropping = False
        if dropping:
            removed.append(line)
        else:
            kept.append(line)
    return "---\n" + "\n".join(kept) + text[end:], removed


def _frontmatter(text: str) -> dict[str, Any]:
    end = text.find("\n---", 4)
    return yaml.safe_load(text[4:end]) or {} if text.startswith("---\n") and end != -1 else {}


def build_plan(vault: Path) -> dict[str, Any]:
    nodes_root = vault / NODES_DIR
    changes = []
    scanned = 0
    for path in sorted(nodes_root.rglob("*.md")):
        scanned += 1
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        new_text, removed = strip_fields(text)
        if not removed:
            continue
        before_meta, after_meta = _frontmatter(text), _frontmatter(new_text)
        expected = {key: value for key, value in before_meta.items() if key not in FIELDS}
        if after_meta != expected:
            raise SystemExit(f"字段移除后其他 frontmatter 发生变化，停止生成计划：{path.relative_to(vault)}")
        changes.append({
            "path": path.relative_to(vault).as_posix(),
            "before_hash": _sha256(raw),
            "after_hash": _sha256(new_text.encode("utf-8")),
            "removed_lines": removed,
        })
    plan = {
        "operation": "remove-node-legacy-fields",
        "fields": list(FIELDS),
        "vault": str(vault.resolve()),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "scanned_files": scanned,
        "changed_files": len(changes),
        "removed_lines": sum(len(item["removed_lines"]) for item in changes),
        "changes": changes,
    }
    plan["plan_hash"] = plan_hash(plan)
    return plan


def plan_hash(plan: dict[str, Any]) -> str:
    stable = {key: plan[key] for key in ("operation", "fields", "changes")}
    return _sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def apply_plan(vault: Path, plan: dict[str, Any], expected_plan_hash: str) -> dict[str, Any]:
    if plan_hash(plan) != expected_plan_hash or plan.get("plan_hash") != expected_plan_hash:
        raise SystemExit("计划哈希不一致：请重新生成并审阅计划")
    stale = []
    for item in plan["changes"]:
        path = vault / item["path"]
        if not path.is_file() or _sha256(path.read_bytes()) != item["before_hash"]:
            stale.append(item["path"])
    if stale:
        raise SystemExit(f"{len(stale)} 个文件自计划生成后已变化，未写入任何文件；请重新生成计划。示例：{stale[:3]}")
    backup = vault / BACKUP_DIR / f"node-field-migration-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    for item in plan["changes"]:
        source = vault / item["path"]
        target = backup / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _sha256(target.read_bytes()) != item["before_hash"]:
            raise SystemExit(f"备份校验失败：{item['path']}，未写入任何文件")
    manifest = {"plan_hash": expected_plan_hash, "created_at": datetime.now().isoformat(timespec="seconds"),
                "changes": [{k: item[k] for k in ("path", "before_hash", "after_hash")} for item in plan["changes"]]}
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    written = 0
    for item in plan["changes"]:
        path = vault / item["path"]
        new_text, _ = strip_fields(path.read_text(encoding="utf-8"))
        data = new_text.encode("utf-8")
        if _sha256(data) != item["after_hash"]:
            raise SystemExit(f"迁移结果与计划不符：{item['path']}；已写入 {written} 个，可用 rollback --backup {backup} 恢复")
        _atomic_write_bytes(path, data)
        written += 1
    return {"written": written, "backup": str(backup)}


def rollback(vault: Path, backup: Path) -> dict[str, Any]:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    restored, skipped = 0, []
    for item in manifest["changes"]:
        path = vault / item["path"]
        current = _sha256(path.read_bytes()) if path.is_file() else ""
        if current == item["before_hash"]:
            continue
        if current != item["after_hash"]:
            skipped.append(item["path"])
            continue
        _atomic_write_bytes(path, (backup / item["path"]).read_bytes())
        restored += 1
    return {"restored": restored, "skipped_changed_after_migration": skipped}


def render_diff(plan: dict[str, Any], vault: Path, limit: int = 5) -> str:
    """Unified diff for the first ``limit`` files, for human review."""
    parts = []
    for item in plan["changes"][:limit]:
        path = vault / item["path"]
        before = path.read_text(encoding="utf-8")
        after, _ = strip_fields(before)
        parts.extend(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{item['path']}", tofile=f"b/{item['path']}", n=1,
        ))
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="移除工作节点中的旧项目级字段（key_point、major_work）")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="只读生成迁移计划")
    plan_cmd.add_argument("--vault", required=True)
    plan_cmd.add_argument("--output", required=True)
    plan_cmd.add_argument("--diff", default="", help="可选：写出前几个文件的 unified diff 供审阅")
    apply_cmd = sub.add_parser("apply", help="按已审阅计划执行，先备份")
    apply_cmd.add_argument("--vault", required=True)
    apply_cmd.add_argument("--plan", required=True)
    apply_cmd.add_argument("--expected-plan-hash", required=True)
    rollback_cmd = sub.add_parser("rollback", help="按备份清单恢复")
    rollback_cmd.add_argument("--vault", required=True)
    rollback_cmd.add_argument("--backup", required=True)
    args = parser.parse_args(argv)
    vault = Path(args.vault)
    if args.command == "plan":
        plan = build_plan(vault)
        Path(args.output).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.diff:
            Path(args.diff).write_text(render_diff(plan, vault), encoding="utf-8")
        summary = {k: plan[k] for k in ("scanned_files", "changed_files", "removed_lines", "plan_hash")}
    else:
        # Hold the same vault lock as normal writes so no AI tool records nodes mid-migration.
        from worklog_core import WorklogStore
        with WorklogStore(vault).mutation_lock():
            if args.command == "apply":
                summary = apply_plan(vault, json.loads(Path(args.plan).read_text(encoding="utf-8")), args.expected_plan_hash)
            else:
                summary = rollback(vault, Path(args.backup))
    print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
