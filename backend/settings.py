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
        "host": "127.0.0.1",
        "port": 8765,
    }
    defaults.update(raw)
    for key in ("vault_root", "weekly_source_dir", "export_dir", "template_path", "node_index_path", "entity_index_path"):
        defaults[key] = str(Path(defaults[key]))
    return defaults
