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

## 覆盖写权限弹窗

`record-intent` 对 Vault 中已有普通文件先保留原文备份，再以 `r+b` 原位覆盖；新文件和其他入口仍可使用临时文件与 `os.replace()`。这减少了部分沙箱把覆盖替换提示为删除的情况。遇到提示先核对目标路径、命令和待写事实，不默认放宽权限。

本版在 WorkBuddy 默认权限下，用跨任务预建的隔离 Vault 完整运行 `record-intent`，无删除确认，完整性检查通过。此结果不保证所有入口和未来权限策略。原位写失去单文件原子性；普通异常会尝试回写原文，强制终止或断电后需核对 `周报系统/备份/node-meta-rewrite/` 中的预写备份。多文件归档本身不是完整事务，不能把单文件回写视为整次归档回滚。
