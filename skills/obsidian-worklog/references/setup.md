# 部署与验收

## 运行前提

仓库内包含脱敏后端和空白周报模板。需准备 Python 环境、个人 `config.yaml` 和可访问的 Obsidian Vault；不要连接原作者的目录或数据。

推荐将 Skill 目录以符号链接安装，入口会根据真实路径找到同仓库的 `backend`。如果复制 Skill 到其他位置，则将 `WORKLOG_ROOT` 设为后端绝对路径。配置中的 Vault、周报模板和导出位置也应逐一改为使用者自己的路径。不要复制包含真实目录、账号或凭据的配置文件。

## 最小验收

1. `python3 scripts/record_worklog.py --help` 能显示命令帮助。
2. `projects --compact --pretty --visible-only --sort-by last_activity` 返回可解析 JSON，且记录来自使用者自己的 Vault。
3. 对一个已知项目运行 `context --project "项目名"`，核对状态、进展、时间和下一步。
4. 用测试 Vault 测试一次录入和修订，再检查原记录、修订链与派生视图。
5. 周报先 `preview --start YYYY-MM-DD --end YYYY-MM-DD`，核实来源后再导出到测试目录。

对只读查询，检查 `meta.returned` 与实际项目数；`meta.truncated: true` 表示只返回一部分，不能据此断言项目不存在。审计有警告时阅读具体警告，不把退出码 0 当作全部通过。
