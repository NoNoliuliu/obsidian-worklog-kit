"""Issue cards, method candidates and formal methods."""
from __future__ import annotations

from worklog_base import (  # noqa: F401
    Any, ConflictError, Iterable, Path, UnknownProjectError, VALID_ISSUE_STATUSES, WorklogError,
    _atomic_write, _extract_section, _file_hash, _normalize, _now, _parse_frontmatter, _safe_yaml, json,
    re, shutil, uuid
)


class KnowledgeMixin:
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
