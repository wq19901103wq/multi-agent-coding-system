# MAC System

MAC 把 Codex 的完整 coding-agent 和原生多 Agent 能力带到飞书，并支持项目目录绑定、移动端
卡片、多 coding-agent 后端、持久会话、计费和本地服务托管。

## 核心行为

- 每个项目对应一个持久 Project Lead Thread；运行时通过统一接口接入，当前默认实现为 Codex；
- Root Agent 第一轮即可直接读写、运行和调试项目；
- 多 Agent 使用 Codex 原生 `spawn_agent`、消息、等待和打断工具；
- 用户运行中补充内容使用 `turn/steer`，idle 后继续同一 thread；
- 原生 `request_user_input` 始终保留飞书问题/审批卡；隔离的 AI Project Owner 在低风险、可撤销
  且依据充分时可把选择回写原卡并在原 turn 中继续；原生审批把决定、依据和调整入口合并在原
  审批卡，不再追加内容重复的决策卡；
- Root 使用 Codex 原生 `workspace-write + on-request`；命令、文件和额外权限审批都投影成飞书卡片，
  Project Owner 可根据同一用户跨项目的明确历史选择代做高把握决定，证据不足时追加评估卡说明；
- 每个成功、失败或中断的用户可见终态展示后，Project Owner 都会独立审阅一次；发现可代做的
  未决事项时追加默认精简、可展开依据的决策卡，需要执行的决定再交回同一 Root Thread；没有
  未决事项时不增加无意义卡片；
- Project Owner 先获得当前真人指令、少量带全部原文来源的 `ProjectPolicy`、全部生效用户偏好和未完成承诺，
  再把当前汇报和最近 5 轮作为执行证据；更早完整历史与纯真人指令历史分别通过只读管理工具分页
  查询。Owner 不读源码，也不代替 Reviewer/Tester/Verifier；`/new` 不会清空管理历史；
- `ProjectPolicy` 只保存明确长期原则或至少两处独立真人来源支持的泛化原则；具体命令和一次性选择
  只保留在项目历史，泛化原则不能代替用户授权；
- 一轮结束后项目保持可迭代，支持新建、修改、debug 和已有目录继续开发；
- Claude Code、Kimi 和 Pi 通过额外后端适配器供 Root Agent 按需调用。

MAC 不包含固定的需求→架构→编码→测试状态机，也没有额外 Coordinator Agent。

## 启动

当前服务托管脚本面向 macOS（launchd）。需要 Python 3.10+、Git，以及已安装并完成认证的
coding-agent CLI；默认执行后端为 Codex。飞书机器人配置见
[接入说明](docs/feishu_onboarding.md)。MAC 本身不附带这些 CLI、认证数据或第三方源码。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env` 填写飞书凭据，分别为 `ADMIN_TOKEN` 和 `CONTROL_API_TOKEN` 设置强随机值。
`.env` 同时被 shell 加载，带空格或特殊字符的值必须正确引用；不要提交此文件。
默认仅监听 `127.0.0.1`；不要把未设管理 token 的服务暴露到网络。
确认认证和配置完成后：

```bash
./scripts/start_server.sh --check
./scripts/start_server.sh
```

脚本优先使用项目根目录的 `.venv`；仅在它不存在时兼容旧的
`.worktrees/feat-core/.venv`。`--check` 只做预检，不启动、停止或重新加载服务。

验证服务：

```bash
lsof -Pi :18080 -sTCP:LISTEN
curl --fail http://127.0.0.1:18080/health
```

浏览管理页时需携带 `?token=<ADMIN_TOKEN>`；避免把实际 token 写进截图、共享 URL 或日志。
管理页认证、文件链接及本地历史数据的边界见 [SECURITY.md](SECURITY.md)。

验证/生产禁止使用不受限的 `uvicorn --reload`，项目文件变化会反复重启服务。

经用户授权的部署重启使用 `bash scripts/restart_server_once.sh <固定操作ID>`。
同一次部署必须始终使用相同 ID；无论成功、失败或被打断，都不会再次执行停服。
入口先运行 `bash scripts/start_server.sh --check` 检查配置及启动依赖，通过后才停服。
启动等待按 120 秒实际耗时设限，接管旧进程前必须确认退出；重复启动不会强杀正在启动的进程。
`.mac/restart_operations/` 保存尝试记录和并发锁，不得为自动重试删除记录、清锁或生成新 ID。
验收必须与部署分开：检查失败只报告，不调用停止/启动脚本；项目切换 Agent 或重置会话
不能触发宿主重启。一次性维护任务禁止配置 `KeepAlive`，完成后必须卸载。
已有日期限定的部署 watchdog 已停用，保留内容仅供审计。

## 常用飞书命令

- `/create` 或 `/c`：创建系统管理目录中的新项目；
- `/open` 或 `/o`：打开已有 Git 项目（兼容 `/bind`、`/b`）；
- `/proj` 或 `/ls`：查看项目；
- `/use` 或 `/s`：切换项目；
- `/info` 或 `/i`：查看当前项目、真实工作目录和状态；
- `/owner` 或 `/ow`：查看 Owner；`/owner off` 关闭当前项目的 Owner，`/owner on` 恢复；
- `/new` 或 `/n`：保留项目和目录，开始新的 Codex 对话；
- `/pause`、`/resume`、`/stop`：暂停、恢复或终止当前项目；
- `/reset`：强制清理当前所选项目的卡住执行及其子进程，取消旧排队任务和 Owner 续推；保留目录、文件、provider 与原生会话历史，不自动重跑，成功后发送新消息继续。与清空对话的 `/new` 不同。只有确认进程退出、宿主任务清理完成才报告成功；清理超时保持项目隔离，可再次 `/reset`。此命令需要 MAC/飞书消息通道仍可响应，不能修复整机或宿主事件循环冻结，也不保证网络/provider 可用。
- `/model`：显示当前 Codex 模型目录、说明和可点击选项，支持翻页和恢复 MAC 默认；也可发送 `/model 模型名`。切换仅在项目空闲时执行，下一条消息开启新对话，项目文件和 Owner 模型不变。当前其他 provider 暂不支持此选择器；获取失败会明确提示，不返回猜测列表。
- `/menu` 或 `/m`：用卡片完成常用操作；
- `/help` 或 `/h`：查看全部快捷命令。

正常项目工作直接使用自然语言，不要求用户选择任务类型、研发阶段或 coding-agent。系统输出使用
卡片；必须询问用户时优先提供选项，同时保留自由输入。

## 代码结构

```text
src/mac_system/
├── agents/root_runtime.py   # Codex app-server 薄客户端
├── core/root_agent.py       # 项目 Root Thread 与飞书投影
├── core/root_progress.py    # 纯内存进度模型与展示
├── core/root_sessions.py    # Root identity / dynamic tool 幂等
├── agents/runtime.py        # 外部 coding-agent 后端
├── feishu/                  # WebSocket、消息和卡片
├── core/project_service.py  # 项目与持久化服务
├── core/project_owner.py    # 独立 Owner 的公开上下文与用户权威
├── utils/                  # 无宿主状态的 ID、用量和脱敏
└── api/                     # FastAPI 与管理页
```

架构见 [docs/architecture.md](docs/architecture.md)，设计原则见
[docs/mac-design-principles.md](docs/mac-design-principles.md)，飞书交互的可验收行为见
[docs/interaction-spec.md](docs/interaction-spec.md)。测试报告必须使用
[docs/testing-taxonomy.md](docs/testing-taxonomy.md) 中的 T1–T9 术语。

## 本地发布准备

开发与验证见 [CONTRIBUTING.md](CONTRIBUTING.md)，发布清单与源码候选包说明见
[docs/release-readiness.md](docs/release-readiness.md)。源码包不包含本机凭据、数据库、用户项目
或旧 Git 历史。本轮代码整理及真实 Kimi 复核见
[docs/architecture-review-20261012.md](docs/architecture-review-20261012.md)。
仓库所有者已授权复核后提交并推送当前分支；许可证尚未选择，公开可见不等于
获得开源使用许可。此授权不包含备份分支、历史重写或部署。
