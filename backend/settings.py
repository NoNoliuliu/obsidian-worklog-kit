from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


BASE_DIR = Path(__file__).resolve().parent


def load_settings() -> dict[str, Any]:
    config_path = BASE_DIR / "config.yaml"
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    defaults = {
        "vault_root": str(BASE_DIR / "vault"),
        "weekly_source_dir": str(BASE_DIR / "imports"),
        "export_dir": str(BASE_DIR / "exports"),
        "template_path": str(BASE_DIR / "assets" / "weekly-template.xlsx"),
        "entity_index_path": str(BASE_DIR / "runtime" / "cache" / "worklog-entity-index.json"),
        "node_index_path": str(BASE_DIR / "runtime" / "cache" / "worklog-node-index.json"),
        "search_exclude": ["个人/", "AI Skill仓库/"],
        "personal_prefixes": ["个人/"],
        "dashboard_dir": str(BASE_DIR / "runtime" / "dashboard"),
    }
    defaults.update(raw)
    defaults["search_exclude"] = [str(item) for item in (defaults.get("search_exclude") or []) if str(item or "").strip()]
    defaults["personal_prefixes"] = [str(item) for item in (defaults.get("personal_prefixes") or []) if str(item or "").strip()]
    for key in ("vault_root", "weekly_source_dir", "export_dir", "template_path", "node_index_path", "entity_index_path", "dashboard_dir"):
        defaults[key] = str(Path(defaults[key]))
    return defaults
