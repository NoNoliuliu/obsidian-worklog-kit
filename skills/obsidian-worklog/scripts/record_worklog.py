from __future__ import annotations

import os
import sys
from pathlib import Path


root_value = os.environ.get("WORKLOG_ROOT")
root = (
    Path(root_value).expanduser().resolve()
    if root_value
    else Path(__file__).resolve().parents[3] / "backend"
)
required = ("worklog_cli.py", "settings.py", "config.yaml")
missing = [name for name in required if not (root / name).is_file()]
if missing:
    raise SystemExit(
        f"后端目录不完整：{root}；缺少 {', '.join(missing)}。"
        "请保留 Skill 到仓库的链接，或设置 WORKLOG_ROOT 为后端目录"
    )

venv_python = root / ".venv-mac" / "bin" / "python"
if not venv_python.is_file():
    raise SystemExit(f"缺少后端虚拟环境：{venv_python}")
if Path(sys.prefix).resolve() != (root / ".venv-mac").resolve():
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(root))
from worklog_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
