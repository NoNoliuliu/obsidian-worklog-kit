"""Retired entrypoint: never load Runtime or perform formal writes."""
from __future__ import annotations

import json


def main(argv: list[str] | None = None) -> int:
    print(json.dumps({
        "ok": False,
        "error_code": "TRAINING_WORKFLOW_RETIRED",
        "error": "培训 Runtime 已于 2026-09-04 停用，obsidian-worklog 已回退到 1.17.0。"
                 "请按普通工作记录流程，通过 record_worklog.py 查询和记录已确认的培训进展；"
                 "不要重试培训同步。原 Runtime 数据仅保留供人工核对，不自动迁移。",
    }, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
