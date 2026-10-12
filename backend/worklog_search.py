"""Full-text search over entities and ordinary vault pages."""
from __future__ import annotations

from worklog_base import (  # noqa: F401
    Any, Iterable, Iterator, Path, WorklogError, _search_normalize, os, re
)
from worklog_inbox import PENDING, list_inbox


class SearchMixin:
    def search(
        self, query: str, limit: int = 20, *, exclude_prefixes: Iterable[str] = (),
        include_pages: bool = True, include_entities: bool = True, only_prefixes: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Search entities and vault pages.

        ``exclude_prefixes`` are vault-relative page folders to skip; ``only_prefixes`` limits the page
        walk to those folders (used for the personal scope together with ``include_entities=False``).
        """
        query = query.strip()
        if not query:
            raise WorklogError("搜索内容不能为空")
        normalized = _search_normalize(query)
        lowered = query.lower()
        results: list[dict[str, Any]] = []

        def add(kind: str, title: str, haystack: str, **extra: Any) -> None:
            if lowered not in haystack.lower() and normalized not in _search_normalize(haystack):
                return
            normalized_title = _search_normalize(title)
            score = 100 if normalized == normalized_title else (70 if normalized in normalized_title else 40)
            results.append({"kind": kind, "title": title, "score": score, "snippet": haystack[:240], **extra})

        if not include_entities:
            for relative, text in self._iter_search_pages(exclude_prefixes, only_prefixes):
                add("page", Path(relative).stem, text, path=relative)
            return sorted(results, key=lambda item: item["score"], reverse=True)[: max(1, min(limit, 100))]
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
        for item in list_inbox(self, status=PENDING):
            add("inbox", Path(item["path"]).stem, " ".join([item.get("text", ""), item.get("project_hint", ""), *item.get("tags", [])]),
                inbox_id=item.get("inbox_id"), path=item["path"], captured_at=item.get("captured_at"))
        for relative, text in (self._iter_search_pages(exclude_prefixes, only_prefixes) if include_pages else ()):
            add("page", Path(relative).stem, text, path=relative)
        return sorted(results, key=lambda item: (item["score"], item["kind"] == "project"), reverse=True)[: max(1, min(limit, 100))]

    def _iter_search_pages(
        self, exclude_prefixes: Iterable[str] = (), only_prefixes: Iterable[str] = (),
    ) -> Iterator[tuple[str, str]]:
        """Yield ordinary vault pages, pruning ledgers, backups and excluded folders before reading."""
        skipped_dirs = {self.backups_dir, self.projects_dir, self.tasks_dir, self.nodes_dir}
        prefixes = tuple(
            item.strip().strip("/") + "/" for item in exclude_prefixes if str(item or "").strip().strip("/")
        )
        structured_types = re.compile(r"^type:\s*['\"]?(project-issue|method-candidate|reusable-method|inbox-item)['\"]?\s*$", re.M)
        roots = [self.vault_root / item.strip().strip("/") for item in only_prefixes if str(item or "").strip().strip("/")]
        walk = (entry for root in (roots or [self.vault_root]) if root.is_dir() for entry in os.walk(root))
        for directory, dirnames, filenames in walk:
            current = Path(directory)
            relative_dir = current.relative_to(self.vault_root).as_posix()
            relative_dir = "" if relative_dir == "." else relative_dir + "/"
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".")
                and current / name not in skipped_dirs
                and not (relative_dir + name + "/").startswith(prefixes)
            )
            for filename in sorted(filenames):
                if not filename.endswith(".md"):
                    continue
                relative = relative_dir + filename
                if prefixes and relative.startswith(prefixes):
                    continue
                path = current / filename
                try:
                    if path.stat().st_size > 1_000_000:
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if text.startswith("---\n"):
                    end = text.find("\n---", 4)
                    if end != -1 and structured_types.search(text[4:end]):
                        continue
                yield relative, text
