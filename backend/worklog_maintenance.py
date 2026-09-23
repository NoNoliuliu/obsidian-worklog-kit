"""Previewed, hash-pinned project maintenance with a durable undo journal.

Native view regeneration happens in a temporary Vault. The real Vault receives
only the reviewed byte diff, under the normal Vault lock; no schema migration.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import quote, unquote, urlsplit

from worklog_core import WorklogStore, WorklogError, ConflictError, _parse_frontmatter_text, _safe_yaml, _safe_filename
from worklog_structure import WORK_DOMAINS, TRAINING_CATEGORIES, DAILY_CATEGORIES, default_project_parent

FORMAT = 1
TEXT_SUFFIXES = {'.md', '.canvas', '.base', '.yaml', '.yml', '.json'}
ROOTS = (*WORK_DOMAINS, '周报系统', '00-工作全景', '方法论库')
SKIP_DIRS = {'.obsidian', '.git', '.trash', 'AI Skill仓库', 'node_modules', '__pycache__'}
PROTECTED_DIRS = {'备份', '周报草稿', '历史快照'}
WIKI = re.compile(r'\[\[([^\]\n]+)\]\]')
MARKDOWN = re.compile(r'(?P<head>!?\[[^\]\n]*\]\()(?P<url><[^>\n]+>|[^\s)]+)(?P<tail>(?:\s+["\'][^\n]*?["\'])?\))')
PATH_FIELD = re.compile(r'(?m)^(?P<head>[ \t]*(?:overview_path|canvas_path|related_path|source_path|file):[ \t]*)(?P<value>[^\n]+)$')


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ConflictError(f'路径已变成目录或符号链接：{path}')
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _json_hash(value: dict) -> str:
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def _safe_path(vault: Path, relative: str) -> Path:
    p = Path(relative)
    if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative or str(p) == '.':
        raise WorklogError(f'必须使用 Vault 内不含 .. 的相对路径：{relative}')
    target = vault / p
    for part in [target, *target.parents]:
        if part == vault:
            break
        if part.is_symlink():
            raise WorklogError(f'维护路径不能穿过符号链接：{relative}')
    if not target.resolve().is_relative_to(vault.resolve()):
        raise WorklogError(f'路径超出 Vault：{relative}')
    return target


def _rel(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def _mapped(path: str, mappings: dict[str, str]) -> str:
    for old in sorted(mappings, key=len, reverse=True):
        if path == old or path.startswith(old + '/'):
            return mappings[old] + path[len(old):]
    return path


def _protected(path: str) -> bool:
    return bool(PROTECTED_DIRS.intersection(Path(path).parts))


def _request(store: WorklogStore, request: dict) -> tuple[dict, dict, dict, list[str]]:
    operation = request.get('operation')
    if operation not in {'move', 'rename'}:
        raise WorklogError('维护操作仅支持 move 或 rename')
    project = store.get_project(str(request.get('project') or ''))
    if not project:
        raise WorklogError('项目不存在，不能静默创建或按近似名称匹配')
    vault = store.vault_root
    overview = str(project.get('overview_path') or '')
    source = ''
    if overview:
        p = _safe_path(vault, overview)
        if not p.is_file():
            raise WorklogError('总览文件不存在，请先核对原位置')
        source = _rel(vault, p.parent)
        parts = Path(source).parts
        min_parts = 3 if parts[0] in {'培训工作', '日常与专项工作'} else 2
        if parts[0] not in WORK_DOMAINS or len(parts) < min_parts or p.name.startswith('00-'):
            raise WorklogError('领域/分类导航目录不能作为单项目目录整体维护')
        for other in store.list_projects():
            raw = str(other.get('overview_path') or '')
            if not raw:
                continue
            other_folder = _safe_path(vault, raw).parent
            other_parts = other_folder.relative_to(vault).parts
            navigation = (Path(raw).name.startswith('00-') and other_parts[0] in WORK_DOMAINS
                          and (len(other_parts) == 1 or (len(other_parts) == 2 and re.match(r'^\d{2}-', other_parts[1]))))
            if navigation:
                continue  # Domain/category index entities do not own all child project folders.
            if other['project_id'] != project['project_id'] and (
                other_folder.is_relative_to(p.parent) or p.parent.is_relative_to(other_folder)
            ):
                raise WorklogError(f'项目目录与其他项目共用或嵌套：{other["name"]}')
    changes = {}
    mappings = {}
    archives = []
    canonical = {'operation': operation, 'project': project['project_id']}
    if operation == 'move':
        if not source:
            raise WorklogError('无总览的轻量事项没有项目目录可搬移')
        domain = str(request.get('to_domain') or project.get('work_domain') or '')
        category = str(request.get('to_category') or project.get('work_category') or '')
        if domain not in WORK_DOMAINS:
            raise WorklogError('目标领域不在支持范围')
        if not category or '/' in category or '\\' in category or category in {'.', '..'}:
            raise WorklogError('目标分类必须是单个分类名称')
        known = {'培训工作': TRAINING_CATEGORIES, '日常与专项工作': DAILY_CATEGORIES}
        if domain in known and category not in known[domain]:
            raise WorklogError('目标分类不在该领域索引中，不能猜测新分类目录')
        destination = str(request.get('to_directory') or (default_project_parent(domain, category) / Path(source).name).as_posix())
        dest = _safe_path(vault, destination)
        if Path(destination).parts[0] != domain or len(Path(destination).parts) < (3 if domain in known else 2):
            raise WorklogError('目标必须是所选领域内的独立项目目录')
        changes = {'work_domain': domain, 'work_category': category,
                   'overview_path': (Path(destination) / Path(overview).name).as_posix()}
        canonical.update(to_domain=domain, to_category=category, to_directory=destination)
        if source != destination:
            mappings[source] = destination
    else:
        name = str(request.get('name') or '').strip()
        if not name or any(ord(c) < 32 for c in name):
            raise WorklogError('新名称不能为空')
        other = store.get_project(name)
        if other and other['project_id'] != project['project_id']:
            raise WorklogError('新名称或别名已被其他项目使用')
        canonical['name'] = name
        changes = {'name': name, 'aliases': sorted({*(project.get('aliases') or []), project['name']} - {name})}
        if source:
            destination = (Path(source).parent / _safe_filename(name, '未命名项目')).as_posix()
            new_overview = (Path(destination) / f'{_safe_filename(name, "未命名项目")}-项目总览.md').as_posix()
            if source != destination:
                mappings[source] = destination
            if overview != new_overview:
                mappings[overview] = new_overview
            changes['overview_path'] = new_overview
        old_ledger = _rel(vault, Path(project['path']))
        new_ledger = _rel(vault, store._project_path(name))
        if old_ledger != new_ledger:
            mappings[old_ledger] = new_ledger
        target_tasks = store.list_tasks(project['project_id'])
        for task in target_tasks:
            old = _rel(vault, Path(task['path']))
            new = _rel(vault, store.tasks_dir / _safe_filename(name, '未命名项目') / Path(old).name)
            if old != new:
                mappings[old] = new
        destination_task_dir = store.tasks_dir / _safe_filename(name, '未命名项目')
        if any(Path(t['path']).is_relative_to(destination_task_dir) and t['project_id'] != project['project_id'] for t in store.list_tasks()):
            raise WorklogError('目标任务目录已有其他项目的任务')
        old_canvas = _rel(vault, store._canvas_path(project['name']))
        new_canvas = _rel(vault, store._canvas_path(name))
        if old_canvas != new_canvas:
            # Map references; the old generated file is archived, not renamed as a source document.
            if (vault / new_canvas).exists():
                raise WorklogError('目标关系图已存在，不覆盖已有文件')
            mappings[old_canvas] = new_canvas
            if (vault / old_canvas).is_file():
                archives.append(old_canvas)
    for old, new in mappings.items():
        _safe_path(vault, old)
        target = _safe_path(vault, new)
        if old.casefold() == new.casefold() and old != new:
            raise WorklogError('仅大小写变化的文件名暂不支持，请分两次使用不同名称')
        if new.startswith(old + '/') or old.startswith(new + '/'):
            raise WorklogError('源目录与目标目录不能互相嵌套')
        if target.exists():
            raise WorklogError(f'目标已存在，不能合并或覆盖：{new}')
    if not mappings and all(project.get(k) == v for k, v in changes.items()):
        raise WorklogError('名称、路径和分类均未变化，无需维护')
    return project, canonical, changes, archives


def _build_mappings(store: WorklogStore, project: dict, request: dict, changes: dict) -> dict[str, str]:
    """The same safe mapping rules as preflight; expanded paths are validated there."""
    vault = store.vault_root
    mappings = {}
    old = project.get('overview_path') or ''
    new = changes.get('overview_path') or old
    if old and old != new:
        if Path(old).parent != Path(new).parent:
            mappings[Path(old).parent.as_posix()] = Path(new).parent.as_posix()
        mappings[old] = new
    if request['operation'] == 'rename':
        name = changes['name']
        pairs = [(_rel(vault, Path(project['path'])), _rel(vault, store._project_path(name))),
                 (_rel(vault, store._canvas_path(project['name'])), _rel(vault, store._canvas_path(name)))]
        pairs += [(_rel(vault, Path(t['path'])), _rel(vault, store.tasks_dir / _safe_filename(name, '未命名项目') / Path(t['path']).name))
                  for t in store.list_tasks(project['project_id'])]
        mappings.update({a: b for a, b in pairs if a != b})
    return mappings


def _capture(vault: Path, scratch: Path, source_folder: str) -> tuple[dict, list[str], list[str]]:
    inventory, directories, skipped = {}, [], []
    roots = [vault / name for name in ROOTS if (vault / name).is_dir()]
    for root in roots:
        if root.is_symlink():
            raise WorklogError(f'业务根目录为符号链接，不能保证隔离：{root.name}')
        for parent, dirs, files in os.walk(root, followlinks=False):
            parent = Path(parent)
            relparent = _rel(vault, parent)
            selected_dirs = []
            for name in sorted(dirs):
                child = parent / name
                rel = _rel(vault, child)
                if name in SKIP_DIRS or name == '备份':
                    skipped.append(rel)
                elif child.is_symlink():
                    if source_folder and (rel == source_folder or rel.startswith(source_folder + '/')):
                        raise WorklogError(f'项目目录含符号链接：{rel}')
                    skipped.append(rel)
                else:
                    selected_dirs.append(name)
            dirs[:] = selected_dirs
            directories.append(relparent)
            (scratch / relparent).mkdir(parents=True, exist_ok=True)
            for name in sorted(files):
                p = parent / name
                rel = _rel(vault, p)
                member = bool(source_folder and (rel.startswith(source_folder + '/')))
                if p.is_symlink():
                    if member:
                        raise WorklogError(f'项目目录含符号链接文件：{rel}')
                    skipped.append(rel)
                    continue
                if p.suffix.lower() not in TEXT_SUFFIXES and not member:
                    skipped.append(rel)
                    continue
                if not p.is_file():
                    raise WorklogError(f'不支持特殊文件：{rel}')
                before = p.stat()
                if before.st_nlink > 1 and member:
                    raise WorklogError(f'项目资料存在硬链接，需单独核对：{rel}')
                target = scratch / rel
                shutil.copy2(p, target)
                after = p.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
                    raise ConflictError(f'读取期间文件发生变化：{rel}')
                inventory[rel] = {'sha256': _digest(target), 'size': before.st_size, 'mode': stat.S_IMODE(before.st_mode)}
    # A skipped subtree inside the project cannot silently be left behind.
    if source_folder and any(p.startswith(source_folder + '/') for p in skipped):
        raise WorklogError('项目目录含被跳过的备份或链接子目录，不能进行不完整搬移')
    return inventory, sorted(directories), sorted(skipped)


class References:
    def __init__(self, vault: Path, inventory: dict, mappings: dict):
        self.vault, self.inventory, self.mappings = vault, inventory, mappings
        self.short = {}
        for path in inventory:
            self.short.setdefault(Path(path).stem, []).append(path)
        self.edits, self.manual, self.preserved = [], [], []

    def target(self, raw: str, source: str, wiki: bool = False) -> tuple[str | None, str, str]:
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.scheme != 'file':
            return None, '', 'external'
        anchor = ('#' + parsed.fragment) if parsed.fragment else ''
        if parsed.query:
            return None, anchor, 'unsupported_query'
        value = unquote(parsed.path)
        if not value:
            return None, anchor, 'anchor'
        if parsed.scheme == 'file' and parsed.netloc not in {'', 'localhost'}:
            return None, anchor, 'external'
        p = Path(value)
        if p.is_absolute():
            if not p.is_relative_to(self.vault):
                return None, anchor, 'external'
            return p.relative_to(self.vault).as_posix(), anchor, 'absolute'
        if wiki and '/' not in value:
            candidates = self.short.get(Path(value).stem, [])
            if len(candidates) != 1:
                return None, anchor, 'ambiguous_short_link'
            return candidates[0], anchor, 'wiki'
        relative_wiki = wiki and value.startswith(('./', '../'))
        relative = Path(value) if wiki and not relative_wiki else Path(source).parent / value
        normal = os.path.normpath(relative.as_posix())
        if normal == '..' or normal.startswith('../'):
            return None, anchor, 'external'
        if wiki:
            if normal not in self.inventory and normal + '.md' in self.inventory:
                normal += '.md'
            if normal not in self.inventory and not relative_wiki:
                local = os.path.normpath((Path(source).parent / value).as_posix())
                if local in self.inventory or local + '.md' in self.inventory:
                    normal = local if local in self.inventory else local + '.md'
                    relative_wiki = True
        return normal, anchor, ('wiki_relative' if relative_wiki else 'wiki') if wiki else 'relative'

    def link(self, raw: str, old_file: str, new_file: str, wiki=False) -> str:
        target, anchor, style = self.target(raw, old_file, wiki)
        if target is None:
            relevant = any(_mapped(p, self.mappings) != p for p in self.short.get(Path(unquote(raw)).stem, []))
            if style in {'ambiguous_short_link', 'unsupported_query'} and (relevant or any(old in raw for old in self.mappings)):
                self.manual.append({'path': new_file, 'reference': raw, 'reason': style})
            return raw
        new_target = _mapped(target, self.mappings)
        if new_target == target and (old_file == new_file or style not in {'relative', 'wiki_relative'}):
            return raw
        if style == 'absolute':
            value = str(self.vault / new_target)
            if raw.startswith('file:'):
                value = (self.vault / new_target).as_uri()
        elif wiki:
            value = new_target
            if not Path(urlsplit(unquote(raw)).path).suffix and value.endswith('.md'):
                value = value[:-3]
        else:
            value = os.path.relpath(new_target, Path(new_file).parent).replace(os.sep, '/')
        if not wiki and not raw.startswith('file:') and ('%' in raw or ' ' in value):
            value = quote(value, safe='/.:@-_$&~!()*+,;=')
        value += anchor
        if value != raw:
            self.edits.append({'path': new_file, 'old': raw, 'new': value})
        return value

    def markdown(self, text: str, old_file: str, new_file: str) -> str:
        parts = re.split(r'(```.*?```|~~~.*?~~~|`[^`\n]+`)', text, flags=re.S)
        return ''.join(part if i % 2 else self._markdown(part, old_file, new_file) for i, part in enumerate(parts))

    def _markdown(self, text: str, old_file: str, new_file: str) -> str:
        def wiki(match):
            value = match.group(1)
            target, sep, label = value.partition('|')
            return '[[' + self.link(target, old_file, new_file, True) + (sep + label if sep else '') + ']]'
        def md(match):
            raw = match['url']; angle = raw.startswith('<')
            value = self.link(raw[1:-1] if angle else raw, old_file, new_file)
            return match['head'] + ('<' + value + '>' if angle else value) + match['tail']
        text = WIKI.sub(wiki, text)
        text = MARKDOWN.sub(md, text)
        def field(match):
            value = match['value'].strip()
            quote_char = value[0] if len(value) > 1 and value[0] == value[-1] and value[0] in {'"', "'"} else ''
            raw = value[1:-1] if quote_char else value
            target = _mapped(raw, self.mappings)
            if target == raw and raw.startswith(str(self.vault) + '/'):
                target = str(self.vault / _mapped(raw[len(str(self.vault)) + 1:], self.mappings))
            if target != raw:
                self.edits.append({'path': new_file, 'old': raw, 'new': target})
            return match['head'] + quote_char + target + quote_char
        text = PATH_FIELD.sub(field, text)
        # Known Bases path predicates are precise references, not prose replacements.
        def base(match):
            raw = match.group(2); new = _mapped(raw, self.mappings)
            if raw != new:
                self.edits.append({'path': new_file, 'old': raw, 'new': new})
            return 'file.inFolder(' + match.group(1) + new + match.group(1) + ')'
        text = re.sub(r'file\.inFolder\((["\'])([^"\']+)\1\)', base, text)
        lines = []
        for line in text.splitlines(keepends=True):
            raw = line.strip()
            target = raw
            if raw in self.inventory or raw in self.mappings:
                target = _mapped(raw, self.mappings)
            elif raw.startswith(str(self.vault) + '/'):
                relative = raw[len(str(self.vault)) + 1:]
                if relative in self.inventory or relative in self.mappings:
                    target = str(self.vault / _mapped(relative, self.mappings))
            if target != raw:
                self.edits.append({'path': new_file, 'old': raw, 'new': target})
                line = line.replace(raw, target, 1)
            lines.append(line)
        return ''.join(lines)

    def rewrite(self, text: str, old_file: str, new_file: str) -> str:
        if _protected(old_file):
            if any(old in text for old in self.mappings):
                self.preserved.append(old_file)
            return text
        if Path(old_file).suffix == '.canvas':
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                self.manual.append({'path': new_file, 'reason': 'invalid_canvas'})
                return text
            original = json.dumps(data, ensure_ascii=False)
            for node in data.get('nodes', []):
                if isinstance(node.get('file'), str):
                    old = node['file']; new = _mapped(old, self.mappings)
                    if old != new:
                        self.edits.append({'path': new_file, 'old': old, 'new': new})
                        node['file'] = new
                if isinstance(node.get('text'), str):
                    node['text'] = self.markdown(node['text'], old_file, new_file)
            return json.dumps(data, ensure_ascii=False, indent=2) + '\n' if original != json.dumps(data, ensure_ascii=False) else text
        # A historical node's factual summary/next action stays verbatim. Only
        # its path field, related-material section and managed heading may change.
        meta, body = _parse_frontmatter_text(text) if Path(old_file).suffix == '.md' else ({}, text)
        if meta.get('type') in {'weekly-node', 'weekly-node-duplicate'}:
            prefix, separator, related = body.partition('## 关联资料')
            updated_meta = dict(meta)
            if isinstance(meta.get('related_path'), str):
                raw = meta['related_path']
                updated_meta['related_path'] = _mapped(raw, self.mappings)
            updated_body = prefix + separator + self.markdown(related, old_file, new_file)
            updated = text if updated_meta == meta and updated_body == body else '---\n' + _safe_yaml(updated_meta) + '\n---\n\n' + updated_body.lstrip()
        else:
            updated = self.markdown(text, old_file, new_file)
        if any(old in updated for old in self.mappings):
            self.manual.append({'path': new_file, 'reason': 'unstructured_old_path_remains'})
        return updated


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): _digest(p) for p in sorted(root.rglob('*'))
            if p.is_file() and '备份' not in p.relative_to(root).parts and p.name != '.worklog.lock'}


@contextmanager
def _prepare(store: WorklogStore, request: dict, planned_at: str):
    project, request, changes, archives = _request(store, request)
    mappings = _build_mappings(store, project, request, changes)
    source_folder = str(Path(project['overview_path']).parent) if project.get('overview_path') else ''
    with tempfile.TemporaryDirectory(prefix='worklog-maintenance-') as temp:
        scratch = Path(temp) / 'vault'; scratch.mkdir()
        inventory, directories, skipped = _capture(store.vault_root, scratch, source_folder)
        ledger_relative = _rel(store.vault_root, Path(project['path']))
        if inventory.get(ledger_relative, {}).get('sha256') != project.get('hash'):
            raise ConflictError('读取项目与复制快照之间台账发生变化，请重新预览')
        before_root = Path(temp) / 'before'
        shutil.copytree(scratch, before_root)
        sim = WorklogStore(scratch, read_only=True)
        for p in sim.list_projects():
            raw = p.get('overview_path') or ''
            if raw:
                _safe_path(scratch, raw)  # Never let the simulated writer reach the real Vault.
        baseline = sim.audit_integrity()
        if not baseline['ok']:
            raise WorklogError('现有完整性审计有错误，先核对后再维护；本命令不代替修复')
        original_states = {p['project_id']: p['project_status'] for p in sim.list_projects()}
        original_tasks = {t['task_id']: (t['project_id'], t['status']) for t in sim.list_tasks()}
        original_nodes = {n['node_id']: (n.get('supersedes'), n.get('status'), n.get('occurred_at'), n.get('summary'))
                          for n in sim.list_nodes(effective_only=False)}
        # Move each inventoried member; other projects' files in a task directory stay put.
        for old in sorted(inventory):
            new = _mapped(old, mappings)
            if old in archives:
                (scratch / old).unlink()
            elif old != new:
                target = scratch / new
                if target.exists():
                    raise WorklogError(f'映射目标冲突：{new}')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(scratch / old), target)
        refs = References(store.vault_root, inventory, mappings)
        for old in sorted(inventory):
            if old in archives:
                continue
            new = _mapped(old, mappings)
            path = scratch / new
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_bytes().decode('utf-8')
            except UnicodeDecodeError:
                refs.manual.append({'path': new, 'reason': 'non_utf8_text'})
                continue
            updated = refs.rewrite(text, old, new)
            if request['operation'] == 'rename' and not _protected(old) and path.suffix == '.md':
                meta, body = _parse_frontmatter_text(updated)
                if meta.get('project_id') == project['project_id'] or old == project.get('overview_path'):
                    new_meta = dict(meta)
                    if 'project_name' in meta:
                        new_meta['project_name'] = changes['name']
                    if old == project.get('overview_path') and '项目名称' in meta:
                        new_meta['项目名称'] = changes['name']
                    kind = meta.get('type')
                    suffix = meta.get('kind') if kind in {'weekly-node', 'weekly-node-duplicate'} else meta.get('title') if kind == 'work-task' else None
                    if suffix is not None:
                        new_body = re.sub(r'(?m)^# [^\n]+$', lambda _: '# ' + changes['name'] + ' · ' + str(suffix), body, count=1)
                    else:
                        labels = {project['name'], str(meta.get('project_name') or ''), str(meta.get('项目名称') or ''), *(project.get('aliases') or [])} - {''}
                        names = '|'.join(re.escape(n) for n in sorted(labels, key=len, reverse=True))
                        new_body = re.sub(r'(?m)^# (?:' + names + r')(?= · |$)', lambda _: '# ' + changes['name'], body, count=1)
                    if new_meta != meta or new_body != body:
                        updated = '---\n' + _safe_yaml(new_meta) + '\n---\n\n' + new_body.lstrip()
            if text != updated:
                path.write_bytes(updated.encode('utf-8'))
        sim = WorklogStore(scratch)
        sim.update_project(project['project_id'], changes)
        # Pin only the maintenance update time, making the reviewed byte diff reproducible.
        ledger = Path(sim.get_project(project['project_id'])['path'])
        meta, body = _parse_frontmatter_text(ledger.read_text())
        meta['updated_at'] = planned_at
        ledger.write_text('---\n' + _safe_yaml(meta) + '\n---\n\n' + body.lstrip())
        sim = WorklogStore(scratch, read_only=True)
        audit = sim.audit_integrity()
        if not audit['ok']:
            raise WorklogError('模拟维护后完整性审计未通过，未修改正式数据')
        if original_states != {p['project_id']: p['project_status'] for p in sim.list_projects()}:
            raise WorklogError('维护不得改变项目生命周期状态')
        if original_tasks != {t['task_id']: (t['project_id'], t['status']) for t in sim.list_tasks()}:
            raise WorklogError('维护不得更改任务身份或状态')
        if original_nodes != {n['node_id']: (n.get('supersedes'), n.get('status'), n.get('occurred_at'), n.get('summary'))
                              for n in sim.list_nodes(effective_only=False)}:
            raise WorklogError('维护不得改写历史节点事实或修订链')
        after = _tree(scratch)
        actions = []
        for relative in sorted(set(inventory) | set(after)):
            before = inventory.get(relative, {}).get('sha256')
            current = after.get(relative)
            if before == current:
                continue
            entry = {'path': relative, 'before_sha256': before, 'after_sha256': current,
                     'action': 'archive' if relative in archives else ('remove' if current is None else ('create' if before is None else 'update'))}
            if Path(relative).suffix in TEXT_SUFFIXES:
                try:
                    a = (before_root / relative).read_text() if before is not None else ''
                    b = (scratch / relative).read_text() if current is not None else ''
                    entry['diff'] = ''.join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), fromfile=relative, tofile=relative))
                except UnicodeDecodeError:
                    pass
            actions.append(entry)
        # An unchanged protected snapshot must not enter the write list.
        if any(_protected(a['path']) for a in actions):
            raise WorklogError('计划会修改或移动历史快照，停止维护')
        moved = [{'from': old, 'to': _mapped(old, mappings), 'sha256': inventory[old]['sha256'],
                  'content_changed': after.get(_mapped(old, mappings)) != inventory[old]['sha256']}
                 for old in sorted(inventory) if _mapped(old, mappings) != old and old not in archives]
        directory_moves = [{'from': d, 'to': _mapped(d, mappings)} for d in directories if _mapped(d, mappings) != d]
        task_cleanup = sorted({str(Path(m['from']).parent) for m in moved if m['from'].startswith('周报系统/任务台账/')
                               and not any(p.startswith(str(Path(m['from']).parent) + '/') for p in [*after, *skipped])})
        engine_hash = _json_hash({name: _digest(Path(__file__).parent / name) for name in (
            'worklog_maintenance.py', 'worklog_core.py', 'worklog_structure.py', 'worklog_cli.py')})
        plan = {'format': FORMAT, 'engine_hash': engine_hash, 'vault': str(store.vault_root.resolve()), 'planned_at': planned_at,
                'request': request, 'project': project['name'], 'changes': changes,
                'input_digest': _json_hash({'files': inventory, 'directories': directories, 'skipped': skipped}),
                'mappings': mappings, 'moved_files': moved, 'directory_moves': directory_moves, 'task_directory_cleanup': task_cleanup, 'actions': actions,
                'references': {'rewritten': refs.edits, 'manual_review': refs.manual, 'preserved_snapshots': sorted(set(refs.preserved))},
                'scan_scope': {'roots': list(ROOTS), 'skipped': skipped, 'files': len(inventory)},
                'validation': {'integrity_ok': True, 'stable_ids_facts_and_statuses': True},
                'summary': {'changed_paths': len(actions), 'moved_files': len(moved), 'reference_edits': len(refs.edits),
                            'manual_review': len(refs.manual), 'archived_canvases': len(archives)}}
        plan['plan_hash'] = _json_hash(plan)
        yield plan, scratch, before_root, directories


def plan_maintenance(store: WorklogStore, request: dict) -> dict:
    with _prepare(store, request, datetime.now().replace(microsecond=0).isoformat()) as (plan, *_):
        return plan


def _validate_plan(plan: dict, expected_hash: str, vault: Path):
    if not isinstance(plan, dict) or plan.get('format') != FORMAT:
        raise WorklogError('不支持的维护计划格式')
    if not re.fullmatch(r'[0-9a-f]{64}', expected_hash or ''):
        raise WorklogError('必须传入已审阅计划的完整 plan_hash')
    payload = {k: v for k, v in plan.items() if k != 'plan_hash'}
    if plan.get('plan_hash') != expected_hash or _json_hash(payload) != expected_hash:
        raise ConflictError('维护计划或预期哈希不一致，请重新预览确认')
    if plan.get('vault') != str(vault.resolve()):
        raise ConflictError('计划不属于当前 Vault')
    for action in plan['actions']:
        _safe_path(vault, action['path'])


def _fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save_journal(transaction: Path, journal: dict):
    temp = transaction / 'journal.tmp'
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(journal, handle, ensure_ascii=False, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, transaction / 'journal.json')
    _fsync_dir(transaction)


@contextmanager
def _lock(vault: Path):
    import fcntl
    path = vault / '周报系统/.worklog.lock'
    if not path.exists() or path.is_symlink():
        raise WorklogError('需要已有 Vault 写锁文件；不得绕过统一写入环境')
    with path.open('r+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConflictError('其他命令正在写入 Vault，请稍后重试原计划') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _copy_checked(source: Path, destination: Path, expected: str):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _digest(destination) != expected:
        raise ConflictError(f'复制校验失败：{source.name}')
    with destination.open('rb') as handle:
        os.fsync(handle.fileno())
    _fsync_dir(destination.parent)


def _transaction(vault: Path, expected_hash: str) -> Path:
    return _safe_path(vault, '周报系统/备份/project-maintenance-' + expected_hash)


def _recovery_check(vault: Path, transaction: Path, journal: dict) -> list[dict]:
    conflicts = []
    for entry in journal['plan']['actions']:
        path = _safe_path(vault, entry['path'])
        current = _digest(path)
        if current not in {entry['before_sha256'], entry['after_sha256']}:
            conflicts.append({'path': entry['path'], 'reason': '文件在操作后被另行修改，不能覆盖'})
        if entry['before_sha256'] is not None and _digest(transaction / 'original' / entry['path']) != entry['before_sha256']:
            conflicts.append({'path': entry['path'], 'reason': '原文件备份缺失或哈希不匹配'})
    return conflicts


def _restore(vault: Path, transaction: Path, journal: dict) -> dict:
    conflicts = _recovery_check(vault, transaction, journal)
    if conflicts:
        journal['status'] = 'recovery_required'; journal['conflicts'] = conflicts
        _save_journal(transaction, journal)
        raise ConflictError(f'恢复遇到外部变更，保留现场和备份：{transaction}')
    journal['status'] = 'rolling_back'; _save_journal(transaction, journal)
    for entry in journal['plan']['actions']:
        path = _safe_path(vault, entry['path'])
        if entry['before_sha256'] is None:
            if path.exists():
                path.unlink(); _fsync_dir(path.parent)
        else:
            staged = transaction / 'restore' / entry['path']
            _copy_checked(transaction / 'original' / entry['path'], staged, entry['before_sha256'])
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, path); _fsync_dir(path.parent)
    for relative in sorted(journal['created_directories'], key=lambda p: len(Path(p).parts), reverse=True):
        path = _safe_path(vault, relative)
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass  # Never remove another writer's new data.
    for relative in journal.get('removed_directories', []):
        _safe_path(vault, relative).mkdir(parents=True, exist_ok=True)
    for entry in journal['plan']['actions']:
        if _digest(_safe_path(vault, entry['path'])) != entry['before_sha256']:
            raise ConflictError('恢复后校验未通过，需保留现场继续核对')
    journal['status'] = 'rolled_back'; _save_journal(transaction, journal)
    return {'status': 'rolled_back', 'transaction': str(transaction), 'restored_paths': len(journal['plan']['actions'])}


def _checkpoint(stage: str):
    """Fault-injection seam for isolated tests; no CLI bypass or failure switch."""


def apply_maintenance(store: WorklogStore, plan: dict, expected_hash: str) -> dict:
    vault = store.vault_root
    _validate_plan(plan, expected_hash, vault)
    transaction = _transaction(vault, expected_hash)
    with _lock(vault):
        if (transaction / 'journal.json').exists():
            journal = json.loads((transaction / 'journal.json').read_text())
            _validate_plan(journal['plan'], expected_hash, vault)
            if journal['status'] == 'committed':
                if all(_digest(_safe_path(vault, e['path'])) == e['after_sha256'] for e in plan['actions']):
                    return {'status': 'committed', 'idempotent_replay': True, 'transaction': str(transaction)}
                raise ConflictError('已提交维护后的文件又有变化，不重放旧计划')
            raise ConflictError('该计划已有未完成或已回退事务，先检查恢复记录；不要重复执行')
        with _prepare(store, plan['request'], plan['planned_at']) as (current, scratch, before, directories):
            if current['plan_hash'] != expected_hash:
                raise ConflictError('预览后文件、引用或目录已变化，请重新预览确认；未写入正式数据')
            created_dirs = set()
            for e in plan['actions']:
                target = _safe_path(vault, e['path'])
                for parent in target.parents:
                    if parent == vault:
                        break
                    if not parent.exists():
                        created_dirs.add(_rel(vault, parent))
            for move in plan['directory_moves']:
                target = _safe_path(vault, move['to'])
                for parent in [target, *target.parents]:
                    if parent == vault:
                        break
                    if not parent.exists():
                        created_dirs.add(_rel(vault, parent))
            transaction.mkdir(parents=True, exist_ok=False)
            journal = {'status': 'preparing', 'plan': plan, 'created_directories': sorted(created_dirs), 'removed_directories': []}
            _save_journal(transaction, journal)
            for e in plan['actions']:
                if e['before_sha256'] is not None:
                    _copy_checked(vault / e['path'], transaction / 'original' / e['path'], e['before_sha256'])
                if e['after_sha256'] is not None:
                    _copy_checked(scratch / e['path'], transaction / 'incoming' / e['path'], e['after_sha256'])
            journal['status'] = 'applying'; _save_journal(transaction, journal)
            try:
                for relative in sorted(created_dirs, key=lambda p: len(Path(p).parts)):
                    _safe_path(vault, relative).mkdir(parents=True, exist_ok=True)
                for e in sorted(plan['actions'], key=lambda e: e['after_sha256'] is None):
                    path = _safe_path(vault, e['path'])
                    if _digest(path) != e['before_sha256']:
                        raise ConflictError(f'提交前路径又发生变化：{e["path"]}')
                    if e['after_sha256'] is None:
                        path.unlink(); _fsync_dir(path.parent)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(transaction / 'incoming' / e['path'], path); _fsync_dir(path.parent)
                    _checkpoint('after_file')
                removals = {d['from'] for d in plan['directory_moves']} | set(plan['task_directory_cleanup'])
                for relative in sorted(removals, key=lambda p: len(Path(p).parts), reverse=True):
                    path = _safe_path(vault, relative)
                    if path.exists():
                        journal['removed_directories'].append(relative); _save_journal(transaction, journal)
                        path.rmdir()
                for e in plan['actions']:
                    if _digest(_safe_path(vault, e['path'])) != e['after_sha256']:
                        raise ConflictError('提交后逐文件哈希或源移除校验失败')
                _checkpoint('before_audit')
                verified = WorklogStore(vault, read_only=True).audit_integrity()
                if not verified['ok']:
                    raise WorklogError('提交后完整性审计未通过')
                journal['status'] = 'committed'; journal['audit_ok'] = True
                _save_journal(transaction, journal)
                return {'status': 'committed', 'idempotent_replay': False, 'transaction': str(transaction),
                        'summary': plan['summary'], 'audit_ok': True, 'manual_review': plan['references']['manual_review']}
            except BaseException as exc:
                journal['failure'] = f'{type(exc).__name__}: {exc}'
                try:
                    _restore(vault, transaction, journal)
                except BaseException as recovery:
                    raise WorklogError(f'维护中断且需要人工核对恢复；备份：{transaction}；{recovery}') from exc
                raise WorklogError(f'维护失败，已恢复原文件；备份：{transaction}；{exc}') from exc


def recover_maintenance(store: WorklogStore, expected_hash: str, *, execute=False) -> dict:
    if not re.fullmatch(r'[0-9a-f]{64}', expected_hash or ''):
        raise WorklogError('需要原维护计划的完整 plan_hash')
    vault = store.vault_root
    transaction = _transaction(vault, expected_hash)
    with _lock(vault):
        path = transaction / 'journal.json'
        if not path.is_file():
            raise WorklogError('找不到该维护事务的恢复记录')
        journal = json.loads(path.read_text())
        _validate_plan(journal['plan'], expected_hash, vault)
        if journal['status'] == 'preparing':
            unchanged = all(_digest(_safe_path(vault, e['path'])) == e['before_sha256'] for e in journal['plan']['actions'])
            if not unchanged:
                raise ConflictError('备份准备阶段后文件另有变化，保留现场，不自动覆盖')
            if execute:
                journal['status'] = 'rolled_back'; _save_journal(transaction, journal)
            return {'status': journal['status'], 'can_restore': True, 'transaction': str(transaction),
                    'message': '尚未修改业务文件，仅取消未完成的备份准备', 'restore_paths': []}
        conflicts = _recovery_check(vault, transaction, journal)
        if not execute:
            return {'status': journal['status'], 'transaction': str(transaction), 'can_restore': not conflicts,
                    'conflicts': conflicts, 'restore_paths': [e['path'] for e in journal['plan']['actions']]}
        if journal['status'] == 'rolled_back':
            return {'status': 'rolled_back', 'idempotent_replay': True, 'transaction': str(transaction)}
        return _restore(vault, transaction, journal)
